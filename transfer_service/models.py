import logging
from django.db import models
from django.db.utils import IntegrityError
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from core_service.models import CustomUser
from egrn_service.models import Store
from byd_service.util import to_python_time
from datetime import datetime
from byd_service.rest import RESTServices
from egrn_service.services import Middleware
from django.core.exceptions import ObjectDoesNotExist


logger = logging.getLogger(__name__)

# Approval status choices for transfer receipts
RECEIPT_APPROVAL_STATUS_CHOICES = [
	('pending_receipt', 'Pending Receipt'),
	('receipt_submitted', 'Receipt Submitted'),
	('approved', 'Approved'),
	('rejected', 'Rejected'),
	('resubmitted', 'Resubmitted'),
]

class MaterialPricing(models.Model):
	"""
	Fallback pricing for materials when SAP ByD doesn't have pricing configured.
	This allows manual price entry for stock transfers.
	"""
	material_id = models.CharField(max_length=50, db_index=True)
	material_name = models.CharField(max_length=255, blank=True)
	unit_price = models.DecimalField(max_digits=15, decimal_places=2)
	currency = models.CharField(max_length=3, default='NGN')
	site_id = models.CharField(max_length=50, blank=True, null=True, help_text="Location/warehouse this price applies to")
	effective_date = models.DateField(default=timezone.now)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
	notes = models.TextField(blank=True, help_text="Source or reason for this pricing")

	class Meta:
		verbose_name = "Material Pricing"
		verbose_name_plural = "Material Pricing"
		ordering = ['-effective_date']
		indexes = [
			models.Index(fields=['material_id', '-effective_date']),
			models.Index(fields=['material_id', 'site_id', '-effective_date']),
		]

	def __str__(self):
		return f"{self.material_id} - {self.currency} {self.unit_price} (effective {self.effective_date})"

def _byd_node(value):
	"""
	Return a ByD OData navigation node as a dict. An expanded association with
	no instance is returned by ByD as {"__deferred": {...}}, which must read as
	empty rather than as a record.
	"""
	if not isinstance(value, dict) or "__deferred" in value:
		return {}
	return value


def _byd_results(value):
	"""
	Normalise a ByD collection navigation to a list: ByD returns either a bare
	list, {"results": [...]}, or a deferred stub when nothing was expanded.
	"""
	if isinstance(value, list):
		return [v for v in value if isinstance(v, dict)]
	if isinstance(value, dict) and "results" in value:
		return [v for v in (value.get("results") or []) if isinstance(v, dict)]
	return []


def _byd_datetime(value):
	"""
	Parse a ByD timestamp. Most nodes use "/Date(1611135136576)/" but
	RequestedFulfillmentPeriod on khsalesorder returns ISO 8601
	("2021-01-21T23:00:00Z"), which to_python_time would silently misread as
	epoch-second 2021. Returns None when the value is unparseable.
	"""
	if not value:
		return None
	text = str(value).strip()
	if "T" in text:
		try:
			return datetime.fromisoformat(text.replace("Z", "+00:00"))
		except ValueError:
			return None
	try:
		return to_python_time(text)
	except (AttributeError, ValueError, OverflowError):
		return None


def _pick_schedule_line(schedule_lines):
	"""
	A sales-order item carries several schedule lines (TypeCode 1 = Requested,
	2 = Confirmed). The store receives against what was ordered, so prefer the
	Requested line; fall back to whatever is first.
	"""
	for wanted in ("1", "2"):
		for line in schedule_lines:
			if str(line.get("TypeCode", "")) == wanted:
				return line
	return schedule_lines[0] if schedule_lines else {}


class InboundDelivery(models.Model):
	"""
	An expected warehouse-to-store receipt, anchored on the SAP ByD Sales Order.

	The store receives against the Sales Order, which only signals intent to ship
	and does not itself deplete inventory. The Outbound Delivery is NOT created up
	front - it is created in ByD only once SCD approves the receipt, for the
	confirmed actual quantity. `object_id` / `delivery_id` are therefore empty
	until that approval happens, and are back-filled at that point.
	"""
	DELIVERY_STATUS_CHOICES = [
		('1', 'Open'),
		('2', 'In Process'),
		('3', 'Completed'),
		('4', 'Cancelled')
	]

	# Anchor: the Sales Order the store receives against.
	# Deliberately not unique=True - legacy rows created from outbound deliveries
	# before the cutover may repeat or omit it. One-row-per-sales-order is enforced
	# in the lookup path instead.
	sales_order_reference = models.CharField(
		max_length=50, null=True, blank=True, db_index=True,
		help_text="SAP ByD Sales Order ID - what the store receives against"
	)
	sales_order_object_id = models.CharField(
		max_length=32, null=True, blank=True,
		help_text="SAP ByD ObjectID of the Sales Order"
	)

	# Outbound delivery identifiers: unknown until SCD approval creates the
	# outbound delivery in ByD, then back-filled. Nullable + unique is safe -
	# MySQL allows multiple NULLs in a unique index.
	object_id = models.CharField(max_length=32, unique=True, null=True, blank=True)
	delivery_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
	delivery_type_code = models.CharField(max_length=10, blank=True, help_text="SAP ByD delivery type code")

	source_location_id = models.CharField(max_length=50, help_text="Warehouse/Location ID from SAP ByD")
	source_location_name = models.CharField(max_length=100, blank=True, help_text="Warehouse/Location name")
	destination_store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='inbound_deliveries')
	delivery_date = models.DateField()
	delivery_status_code = models.CharField(max_length=1, choices=DELIVERY_STATUS_CHOICES, default='1')
	metadata = models.JSONField(default=dict)
	created_date = models.DateTimeField(auto_now_add=True)
	
	@property
	def delivery_status(self):
		"""
		Get delivery status display
		"""
		return dict(self.DELIVERY_STATUS_CHOICES).get(self.delivery_status_code, 'Unknown')
	
	@property
	def total_quantity_expected(self):
		"""
		Calculate total quantity expected from all line items
		"""
		return sum(item.quantity_expected for item in self.line_items.all())
	
	@property
	def total_quantity_received(self):
		"""
		Calculate total quantity already received
		"""
		return sum(item.quantity_received for item in self.line_items.all())
	
	@property
	def is_fully_received(self):
		"""
		Check if delivery is fully received
		"""
		total_expected = self.total_quantity_expected
		if total_expected == 0:
			return False  # Cannot be fully received if there's nothing expected
		return self.total_quantity_received >= total_expected
	
	@classmethod
	def create_from_byd_data(cls, delivery_data):
		"""
		Create an inbound delivery from SAP ByD data (warehouse-to-store)
		"""
		# Validate required fields
		required_fields = ["ObjectID", "ID"]
		for field in required_fields:
			if field not in delivery_data:
				raise ValidationError(f"Required field '{field}' missing from delivery data")
		
		# Create new delivery instance
		delivery = cls()
		delivery.object_id = delivery_data["ObjectID"]
		delivery.delivery_id = delivery_data["ID"]
		# Force the delivery status code to 1 (Open)
		delivery.delivery_status_code = "1"
		delivery.delivery_type_code = delivery_data.get("DeliveryTypeCode", "")
		
		# Handle date conversion - use shipping period or creation date
		if "ShippingPeriod" in delivery_data and delivery_data["ShippingPeriod"]:
			shipping_period = delivery_data["ShippingPeriod"]
			if "StartDateTime" in shipping_period:
				delivery_datetime = to_python_time(shipping_period["StartDateTime"])
				delivery.delivery_date = delivery_datetime.date() if hasattr(delivery_datetime, 'date') else delivery_datetime
			else:
				delivery.delivery_date = timezone.now().date()
		elif "CreationDateTime" in delivery_data:
			creation_datetime = to_python_time(delivery_data["CreationDateTime"])
			delivery.delivery_date = creation_datetime.date() if hasattr(creation_datetime, 'date') else creation_datetime
		else:
			delivery.delivery_date = timezone.now().date()
		
		# Extract warehouse location information (source)
		ship_from_location = delivery_data.get("ShipFromLocation", {})
		if ship_from_location:
			delivery.source_location_id = ship_from_location.get("LocationID", "")
			# ShipFromLocation only exposes the ID; the descriptive name lives on the
			# Location master data collection (khlocation/LocationCollection).
			location = RESTServices().get_location_by_id(delivery.source_location_id)
			delivery.source_location_name = (
				location.get("Name") if location and location.get("Name")
				else f"Warehouse {delivery.source_location_id}"
			)
		
		# Extract destination store information
		product_recipient_party = delivery_data.get("ProductRecipientParty", {})
		if not product_recipient_party:
			raise ValidationError("ProductRecipientParty (destination store) information missing from delivery")
		
		dest_store_code = product_recipient_party.get("PartyID")
		if not dest_store_code:
			raise ValidationError("Destination store PartyID missing from delivery")
		
		try:
			delivery.destination_store = cls._find_store_by_identifier(dest_store_code)
		except Store.DoesNotExist:
			raise ValidationError(f"Destination store not found for delivery {delivery.delivery_id}: {dest_store_code}")
		
		# Store sales order reference if available
		delivery.sales_order_reference = delivery_data.get("SalesOrderID")
		
		# Store metadata
		delivery.metadata = delivery_data
		
		try:
			delivery.save()
			
			# Create line items
			if "Item" in delivery_data:
				delivery.__create_line_items__(delivery_data["Item"])
			
			logger.info(f"Created delivery {delivery.delivery_id} from warehouse {delivery.source_location_id} to store {delivery.destination_store.store_name}")
			return delivery
			
		except IntegrityError as e:
			logger.error(f"Error creating delivery {delivery.delivery_id}: {e}")
			raise ValidationError(f"Error creating delivery: {e}")
	
	@classmethod
	def create_from_sales_order_data(cls, sales_order_data):
		"""
		Create an expected receipt from a SAP ByD Sales Order (the post-cutover flow).

		No Outbound Delivery exists at this point, so object_id / delivery_id are
		left empty; they are back-filled when SCD approval creates the outbound
		delivery in ByD.

		Field mappings verified against khsalesorder/SalesOrderCollection payloads
		(ByD tenant my350679, Sept 2026):
		  - destination store: ProductRecipientParty.PartyID (header/item). On
		    current intracompany transfers BuyerParty is the company (FC-0001);
		    on older orders it is the store account (1468), so it is the fallback.
		  - source warehouse: Item/ItemShipFromLocation.LocationID (e.g. 13101).
		    SalesUnitParty is NOT reliable - it is often the company itself (1000).
		  - quantities: Item/ItemScheduleLine is a list of {TypeCode 1=Requested,
		    2=Confirmed}; unit lives in the lowercase key ``unitCode``.
		  - items with CancellationStatusCode "4" (Canceled) are skipped.
		Expanded navigations that are empty come back as ``{"__deferred": ...}``,
		which every lookup below treats as absent.
		"""
		for field in ("ObjectID", "ID"):
			if field not in sales_order_data:
				raise ValidationError(f"Required field '{field}' missing from sales order data")

		delivery = cls()
		delivery.sales_order_reference = sales_order_data["ID"]
		delivery.sales_order_object_id = sales_order_data["ObjectID"]
		# object_id / delivery_id intentionally left NULL until SCD approval.
		delivery.delivery_status_code = "1"
		delivery.delivery_type_code = ""

		# Delivery date: requested fulfilment window, else creation date, else today.
		requested_period = _byd_node(sales_order_data.get("RequestedFulfillmentPeriod"))
		raw_date = (
			requested_period.get("StartDateTime")
			or requested_period.get("EndDateTime")
			or sales_order_data.get("RequestedFulfillmentDate")
			or sales_order_data.get("CreationDateTime")
		)
		parsed = _byd_datetime(raw_date) if raw_date else None
		delivery.delivery_date = parsed.date() if parsed else timezone.now().date()

		items = _byd_results(sales_order_data.get("Item"))

		# Source warehouse - the ship-from site on the items; SalesUnitParty is
		# frequently the company (1000) rather than a warehouse, so it is only a
		# fallback.
		delivery.source_location_id = ""
		for item in items:
			ship_from = _byd_node(item.get("ItemShipFromLocation"))
			if ship_from.get("LocationID"):
				delivery.source_location_id = ship_from["LocationID"]
				break
		if not delivery.source_location_id:
			delivery.source_location_id = (
				_byd_node(sales_order_data.get("SalesUnitParty")).get("PartyID")
				or _byd_node(sales_order_data.get("SellerParty")).get("PartyID")
				or ""
			)
		if delivery.source_location_id:
			location = RESTServices().get_location_by_id(delivery.source_location_id)
			delivery.source_location_name = (
				location.get("Name") if location and location.get("Name")
				else f"Warehouse {delivery.source_location_id}"
			)

		# Destination store - on current intracompany transfers BuyerParty is the
		# company itself (FC-0001) and the store is the ProductRecipientParty
		# (header, then item); older orders carry the store's customer account
		# as BuyerParty, so that is tried last.
		candidate_codes = []
		for party in (
			_byd_node(sales_order_data.get("ProductRecipientParty")),
			*(_byd_node(item.get("ItemProductRecipientParty")) for item in items),
			_byd_node(sales_order_data.get("BuyerParty")),
		):
			code = party.get("PartyID")
			if code and code not in candidate_codes:
				candidate_codes.append(code)
		if not candidate_codes:
			raise ValidationError(
				f"Destination store PartyID missing from sales order {delivery.sales_order_reference}"
			)
		delivery.destination_store = None
		for code in candidate_codes:
			try:
				delivery.destination_store = cls._find_store_by_identifier(code)
				break
			except Store.DoesNotExist:
				continue
		if delivery.destination_store is None:
			raise ValidationError(
				f"Destination store not found for sales order "
				f"{delivery.sales_order_reference}: tried {', '.join(candidate_codes)}"
			)

		delivery.metadata = sales_order_data

		try:
			delivery.save()

			# CancellationStatusCode 4 = Canceled - nothing will ship for it.
			active_items = [i for i in items if str(i.get("CancellationStatusCode", "")) != "4"]
			if active_items:
				delivery.__create_line_items__(active_items)

			logger.info(
				f"Created expected receipt for sales order {delivery.sales_order_reference} "
				f"from {delivery.source_location_id} to store {delivery.destination_store.store_name}"
			)
			return delivery
		except IntegrityError as e:
			logger.error(f"Error creating receipt for sales order {delivery.sales_order_reference}: {e}")
			raise ValidationError(f"Error creating receipt: {e}")

	@staticmethod
	def _find_store_by_identifier(identifier):
		from django.db.models import Q
		"""
		Find store by various identifier fields
		"""
		store = Store
		try:
			# Find the store in the DB by the byd_cost_center_code OR the byd_bill_to_party_id key in the metadata
			delivery_store = store.objects.get(Q(byd_cost_center_code=identifier) | Q(metadata__contains={'byd_bill_to_party_id': identifier}))
		except ObjectDoesNotExist:
			middleware = Middleware()
			store_data = middleware.get_store(byd_bill_to_party_id=identifier)
			if not store_data:
				store_data = middleware.get_store(byd_cost_center_code=identifier)
			
			# If the store is found, use the store data to create a new store
			if store_data:
				delivery_store = store().create_store(store_data[0])
			else:
				raise Store.DoesNotExist("Store not found.")

		return delivery_store
	
	def __create_line_items__(self, line_items_data):
		"""
		Create line items for this delivery from SAP ByD outbound delivery data
		"""
		from decimal import Decimal

		for item_data in line_items_data:
			line_item = InboundDeliveryLineItem()
			line_item.delivery = self
			# On a sales order the product sits under ItemProduct; on an outbound
			# delivery it is on the item itself.
			item_product = item_data.get("ItemProduct") or {}

			line_item.object_id = item_data.get("ObjectID", "")
			line_item.product_id = item_data.get("ProductID") or item_product.get("ProductID", "")

			# For product name, try to get from ProductDescription or Description (from product details) or construct from ProductID
			line_item.product_name = (
				item_data.get("ProductDescription") or
				item_data.get("Description") or
				item_product.get("ProductDescription") or
				item_product.get("Description") or
				line_item.product_id or
				"Unknown Product"
			)

			# Quantity: outbound deliveries carry ItemDeliveryQuantity; sales orders
			# carry the intended ship quantity on ItemScheduleLine.
			item_delivery_quantity = _byd_node(item_data.get("ItemDeliveryQuantity"))
			schedule_line = _pick_schedule_line(_byd_results(item_data.get("ItemScheduleLine")))

			if item_delivery_quantity:
				quantity = item_delivery_quantity.get("Quantity", "0")
				unit_code = item_delivery_quantity.get("UnitCode", "")
				unit_text = item_delivery_quantity.get("UnitCodeText", unit_code)
			elif schedule_line:
				quantity = schedule_line.get("Quantity", "0")
				# khsalesorder spells the unit with a lowercase u: "unitCode".
				unit_code = (
					schedule_line.get("unitCode")
					or schedule_line.get("QuantityUnitCode")
					or schedule_line.get("UnitCode", "")
				)
				unit_text = (
					schedule_line.get("unitCodeText")
					or schedule_line.get("QuantityUnitCodeText")
					or unit_code
				)
			else:
				# Fallback to direct quantity field
				quantity = item_data.get("RequestedQuantity") or item_data.get("Quantity", "0")
				unit_code = item_data.get("QuantityUnitCode", "")
				unit_text = unit_code

			line_item.quantity_expected = float(quantity)
			line_item.unit_of_measurement = unit_text if unit_text else unit_code

			# Extract unit price from various possible SAP ByD fields
			# First check for our enriched unit_price from material valuation
			unit_price = (
				item_data.get("unit_price") or
				item_data.get("ListUnitPriceAmount") or
				item_data.get("NetUnitPriceAmount") or
				item_data.get("UnitPrice") or
				item_data.get("NetAmount") or
				0
			)
			# Handle if price is nested in an Amount structure
			if isinstance(unit_price, dict):
				unit_price = unit_price.get("content") or unit_price.get("Amount") or 0
			line_item.unit_price = Decimal(str(unit_price))

			line_item.metadata = item_data
			line_item.save()
	
	def __str__(self):
		# delivery_id is empty until SCD approval creates the outbound delivery.
		reference = self.delivery_id or f"SO {self.sales_order_reference}"
		return f"{reference} - Warehouse {self.source_location_id} to {self.destination_store.store_name}"
	
	class Meta:
		verbose_name = "Inbound Delivery"
		verbose_name_plural = "Inbound Deliveries"


class InboundDeliveryLineItem(models.Model):
	"""
	Individual line items for inbound deliveries
	"""
	delivery = models.ForeignKey(InboundDelivery, on_delete=models.CASCADE, related_name='line_items')
	object_id = models.CharField(max_length=32)
	product_id = models.CharField(max_length=32)
	product_name = models.CharField(max_length=100)
	quantity_expected = models.DecimalField(max_digits=15, decimal_places=3)
	quantity_received = models.DecimalField(max_digits=15, decimal_places=3, default=0)
	unit_of_measurement = models.CharField(max_length=32)
	unit_price = models.DecimalField(max_digits=15, decimal_places=3, default=0, help_text="Unit price from SAP ByD")
	metadata = models.JSONField(default=dict)
	
	@property
	def quantity_outstanding(self):
		"""
		Calculate outstanding quantity to be received
		"""
		from decimal import Decimal
		return self.quantity_expected - Decimal(str(self.quantity_received))
	
	@property
	def is_fully_received(self):
		"""
		Check if line item is fully received
		"""
		return self.quantity_received >= self.quantity_expected
	
	def __str__(self):
		return f"{self.product_name} - {self.quantity_expected} {self.unit_of_measurement}"
	
	class Meta:
		verbose_name = "Inbound Delivery Line Item"
		verbose_name_plural = "Inbound Delivery Line Items"


class TransferReceiptNote(models.Model):
	"""
	Records goods received at destination store
	"""
	inbound_delivery = models.ForeignKey(InboundDelivery, on_delete=models.CASCADE, related_name='receipts')
	receipt_number = models.IntegerField(unique=True)
	notes = models.TextField(blank=True, null=True)
	created_date = models.DateField(auto_now_add=True)
	created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
	metadata = models.JSONField(default=dict)

	# Approval workflow fields
	approval_status = models.CharField(
		max_length=25,
		choices=RECEIPT_APPROVAL_STATUS_CHOICES,
		default='pending_receipt',
		help_text="Current approval status in the two-stage workflow"
	)
	submitted_at = models.DateTimeField(null=True, blank=True, help_text="When receiving store submitted the receipt")
	approved_at = models.DateTimeField(null=True, blank=True, help_text="When sending store approved the receipt")
	approved_by = models.ForeignKey(
		CustomUser,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='approved_transfer_receipts',
		help_text="User from sending store who approved"
	)
	rejection_reason = models.TextField(blank=True, null=True, help_text="Reason for rejection by sending store")
	rejection_count = models.IntegerField(default=0, help_text="Number of times receipt has been rejected")
	synced_to_sap = models.BooleanField(default=False, help_text="Whether receipt has been synced to SAP ByD")
	
	@property
	def total_quantity_received(self):
		"""
		Total quantity received in this transfer receipt
		"""
		return sum(float(item.quantity_received) for item in self.line_items.all())
	
	@property
	def total_value_received(self):
		"""
		Total value of goods received
		"""
		return sum(item.value_received for item in self.line_items.all())
	
	def save(self, *args, **kwargs):
		"""
		Generate unique receipt number on creation
		"""
		if not self.receipt_number:
			count = TransferReceiptNote.objects.filter(inbound_delivery=self.inbound_delivery).count()
			# Sales-order receipts (eGRN 2) have no delivery_id until SCD approval
			# creates the outbound delivery, so number them off the sales order.
			prefix = self.inbound_delivery.delivery_id or self.inbound_delivery.sales_order_reference
			prefix = ''.join(ch for ch in str(prefix or '') if ch.isdigit()) or str(self.inbound_delivery.id)
			self.receipt_number = f"{prefix}{count + 1}"

		super().save(*args, **kwargs)
		return self
	
	def __str__(self):
		return f"TR-{self.receipt_number}"
	
	class Meta:
		verbose_name = "Transfer Receipt Note"
		verbose_name_plural = "Transfer Receipt Notes"


class TransferReceiptLineItem(models.Model):
	"""
	Individual items in a transfer receipt note
	"""
	transfer_receipt = models.ForeignKey(TransferReceiptNote, on_delete=models.CASCADE, related_name='line_items')
	inbound_delivery_line_item = models.ForeignKey(InboundDeliveryLineItem, on_delete=models.CASCADE, related_name='transfer_receipt_items')
	quantity_received = models.DecimalField(max_digits=15, decimal_places=3)
	metadata = models.JSONField(default=dict)
	
	@property
	def value_received(self):
		"""
		Calculate value of received goods based on unit price from delivery line item
		"""
		unit_price = self.inbound_delivery_line_item.unit_price or 0
		return float(self.quantity_received) * float(unit_price)
	
	@property
	def product_name(self):
		return self.inbound_delivery_line_item.product_id
	
	@property
	def product_id(self):
		return self.inbound_delivery_line_item.product_id
	
	def clean(self):
		"""
		Validate quantity received doesn't exceed quantity issued
		"""
		# Get total already received for this goods issue line item
		existing_received = self.inbound_delivery_line_item.transfer_receipt_items.exclude(
			id=self.id
		).aggregate(total=Sum('quantity_received'))['total'] or 0
		
		total_to_receive = float(existing_received) + float(self.quantity_received)
		
		if total_to_receive > float(self.inbound_delivery_line_item.quantity_expected):
			raise ValidationError(
				f"Cannot receive {self.quantity_received}. "
				f"Available quantity: {float(self.inbound_delivery_line_item.quantity_expected) - float(existing_received)}"
			)
	
	def save(self, *args, **kwargs):
		self.clean()
		super().save(*args, **kwargs)
	
	def __str__(self):
		return f"TR-{self.transfer_receipt.receipt_number}: {self.product_name} ({self.quantity_received})"
	
	class Meta:
		verbose_name = "Transfer Receipt Line Item"
		verbose_name_plural = "Transfer Receipt Line Items"



class StoreAuthorization(models.Model):
	"""
	Links users to authorized stores with roles
	"""
	STORE_ROLE_CHOICES = [
		('manager', 'Store Manager'),
		('assistant', 'Assistant Manager'),
		('clerk', 'Store Clerk'),
		('viewer', 'Viewer Only'),
	]
	
	user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='store_authorizations')
	store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='authorized_users')
	role = models.CharField(max_length=50, choices=STORE_ROLE_CHOICES)
	created_date = models.DateTimeField(auto_now_add=True)

	class Meta:
		unique_together = ('user', 'store')
		verbose_name = "Store Authorization"
		verbose_name_plural = "Store Authorizations"

	def __str__(self):
		return f"{self.user.username} - {self.store.store_name} ({self.role})"


class SourceLocationAuthorization(models.Model):
	"""
	Links users (typically SCD_Team members) to authorized source locations
	(warehouses) with roles. Mirrors StoreAuthorization, which scopes users to
	destination stores; this scopes them to the warehouses they may approve
	receipts from.
	"""
	SOURCE_ROLE_CHOICES = [
		('approver', 'Approver'),
		('viewer', 'Viewer Only'),
	]

	user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='source_location_authorizations')
	source_location_id = models.CharField(max_length=50, help_text="Warehouse/Location ID from SAP ByD")
	source_location_name = models.CharField(max_length=100, blank=True, help_text="Auto-populated from SAP on save")
	role = models.CharField(max_length=50, choices=SOURCE_ROLE_CHOICES, default='approver')
	created_date = models.DateTimeField(auto_now_add=True)

	class Meta:
		unique_together = ('user', 'source_location_id')
		verbose_name = "Source Location Authorization"
		verbose_name_plural = "Source Location Authorizations"

	def __str__(self):
		label = self.source_location_name or self.source_location_id
		return f"{self.user.username} - {label} ({self.role})"

	def save(self, *args, **kwargs):
		# Auto-populate the location name from SAP when missing or when the ID changes.
		if self.source_location_id and not self.source_location_name:
			try:
				location = RESTServices().get_location_by_id(self.source_location_id)
				if location and location.get("Name"):
					self.source_location_name = location["Name"]
			except Exception as e:
				logger.warning(f"Could not fetch location name for {self.source_location_id}: {e}")
		super().save(*args, **kwargs)