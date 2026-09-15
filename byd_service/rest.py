import os
import json
import re
import logging
import time
from requests import get, post, auth as requests_auth
from pathlib import Path
from dotenv import load_dotenv
from django.utils import timezone
from django.core.cache import cache

from .authenticate import SAPAuthentication

dotenv_path = os.path.join(Path(__file__).resolve().parent.parent, '.env')
load_dotenv(dotenv_path)

logger = logging.getLogger(__name__)

# Initialize the authentication class
sap_auth = SAPAuthentication()

@sap_auth.http_authentication
class RESTServices:
	'''
		RESTful API for interacting with SAP's ByD system
	'''
	
	endpoint = os.getenv('SAP_URL')
	# Initialize a CSRF token to None initially
	session = None
	# Initialize headers that are required for authentication
	auth_headers = {}
	# Initialize the SAP token to None initially
	auth = None
	
	comm_auth = requests_auth.HTTPBasicAuth(
		os.getenv('SAP_COMM_USER'),
		os.getenv('SAP_COMM_PASS'),
	)

	def __init__(self):
		self.last_token_refresh = 0
		self.token_refresh_interval = 300  # 5 minutes
	
	def refresh_csrf_token(self):
		"""Refresh the CSRF token if it's been more than 5 minutes since the last refresh"""
		current_time = time.time()
		if current_time - self.last_token_refresh > self.token_refresh_interval:
			try:
				action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khpurchaseorder/"
				headers = {"x-csrf-token": "fetch"}
				response = self.session.get(action_url, auth=self.auth, headers=headers, timeout=30)
				if response.status_code == 200:
					self.auth_headers['x-csrf-token'] = response.headers.get('x-csrf-token', '')
					self.last_token_refresh = current_time
					logger.info("CSRF token refreshed successfully")
				else:
					logger.error(f"Failed to refresh CSRF token. Status code: {response.status_code}")
					raise Exception(f"Failed to refresh CSRF token. Status code: {response.status_code}")
			except Exception as e:
				logger.error(f"Error refreshing CSRF token: {str(e)}")
				raise
	
	def check_object_lock(self, object_id: str, object_type: str) -> bool:
		"""Check if an object is locked in SAP ByD"""
		try:
			if object_type == 'delivery':
				check_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khinbounddelivery/InboundDeliveryCollection('{object_id}')"
			elif object_type == 'invoice':
				check_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khsupplierinvoice/SupplierInvoiceCollection('{object_id}')"
			else:
				raise ValueError(f"Unsupported object type: {object_type}")
			
			response = self.session.get(check_url, auth=self.auth, timeout=30)
			return response.status_code == 423  # 423 means object is locked
		except Exception as e:
			logger.error(f"Error checking object lock: {str(e)}")
			return False
	
	def __get__(self, *args, **kwargs):
		self.refresh_csrf_token()
		return self.session.get(*args, **kwargs, auth=self.auth)
	
	def __post__(self, *args, **kwargs):
		'''
			This method makes a POST request to the given URL with CSRF protection
		'''
		self.refresh_csrf_token()
		headers = {
			'Accept': 'application/json',
			'Content-Type': 'application/json'
		}
		headers.update(self.auth_headers)
		return self.session.post(*args, **kwargs, headers=headers, auth=self.auth)
	
	def get_store_by_params(self, **kwargs):
		action_url = f"{self.endpoint}"

	def get_vendor_by_id(self, vendor_id, id_type='email'):
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khbusinesspartner/CurrentDefaultAddressInformationCollection?$format=json&$expand=EMail,BusinessPartner,ConventionalPhone,MobilePhone&$select=EMail,BusinessPartner,ConventionalPhone,MobilePhone&$top=10"
		id_type = id_type.lower()

		if id_type not in ['email', 'phone', 'internal_id']:
			raise ValueError(f"Unsupported ID type: {id_type}")

		if id_type == 'phone':
			vendor_id = vendor_id.strip()[-10:]
			query_url = f"{action_url}&$filter=substringof('{vendor_id}',ConventionalPhone/NormalisedNumberDescription)"
		elif id_type == 'email':
			query_url = f"{action_url}&$filter=EMail/URI eq '{vendor_id}'"
		elif id_type == 'internal_id':
			query_url = f"{action_url}&$filter=BusinessPartner/InternalID eq '{vendor_id}'"
		
		# Make a request with HTTP Basic Authentication
		response = self.__get__(query_url)
		
		if response.status_code == 200:
			try:
				response_json = json.loads(response.text)
			except Exception as e:
				raise e

			results = response_json["d"]["results"]

			if results:
				active = list(
					filter(lambda x: int(x['BusinessPartner']['LifeCycleStatusCode'])==2, results)
				)
				return active[0] if active else False

		return False

	def get_vendor_purchase_orders(self, internal_id):
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khpurchaseorder/PurchaseOrderCollection?$format=json&$expand=Supplier,Item&$filter=Supplier/PartyID eq '{internal_id}'"

		# Make a request with HTTP Basic Authentication
		response = self.__get__(action_url)

		if response.status_code == 200:
			try:
				response_json = json.loads(response.text)
				results = response_json["d"]["results"]

				# Keys to unset
				keys_to_unset = ['AttachmentFolder', 'Notes', 'PaymentTerms', 'BuyerParty', 'BillToParty',
								 'EmployeeResponsible', 'PurchasingUnit', 'Supplier', '__metadata']
				for result in results:
					# Unset keys from the dictionary
					for key in keys_to_unset:
						if key in result:
							del result[key]
				return results
			except Exception as e:
				raise e

		return False

	def get_purchase_order_by_id(self, PurchaseOrderID):
		action_url: str = (f"{self.endpoint}/sap/byd/odata/cust/v1/khpurchaseorder/PurchaseOrderCollection?$format=json"
						   f"&$expand=Supplier/SupplierName,Supplier/SupplierFormattedAddress,"
						   f"BuyerParty,BuyerParty/BuyerPartyName,"
						   f"Supplier/SupplierPostalAddress,"
						   f"ApproverParty/ApproverPartyName,"
						   f"Item/ItemShipToLocation/DeliveryAddress/DeliveryPostalAddress&$filter=ID eq '"
						   f"{PurchaseOrderID}'")

		# Make a request with HTTP Basic Authentication
		response = self.__get__(action_url)

		if response.status_code == 200:
			try:
				response_json = json.loads(response.text)
				results = response_json["d"]["results"]
				return results[0] if results else False
			except Exception as e:
				raise e

		return False
	
	# GRN Creation
	def create_grn(self, grn_data: dict) -> dict:
		'''
			Create a Goods and Service Acknowledgement (GRN) in SAP ByD
		'''
		
		# Action URL for creating a Goods and Service Acknowledgement (GRN) in SAP ByD
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khgoodsandserviceacknowledgement/GoodsAndServiceAcknowledgementCollection"
		
		try:
			# Make a request with HTTP Basic Authentication
			response = self.__post__(action_url, json=grn_data)
			if response.status_code == 201:
				logging.info(f"GRN successfully created in SAP ByD.")
				return response.json()
			else:
				logging.error(f"Failed to create GRN: {response.text}")
				raise Exception(f"Error from SAP: {response.text}")
		except Exception as e:
			raise Exception(f"Error creating GRN: {e}")
	
	def post_grn(self, object_id: str) -> dict:
		'''
			Post a Goods and Service Acknowledgement (GRN) in SAP ByD
		'''
		# Action URL for creating a Goods and Service Acknowledgement (GRN) in SAP ByD
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khgoodsandserviceacknowledgement/SubmitForRelease?ObjectID='{object_id}'"
		try:
			# Make a request with HTTP Basic Authentication
			response = self.__post__(action_url)
			if response.status_code == 200:
				logging.info(f"GRN successfully POSTED.")
				return response.json()
			else:
				logging.error(f"Failed to create GRN: {response.text}")
				raise Exception(f"Error from SAP: {response.text}")
		except Exception as e:
			raise Exception(f"Error creating GRN: {e}")
	
	# Supplier Invoice Creation
	def create_supplier_invoice(self, invoice_data: dict) -> dict:
		'''
			Create a Supplier Invoice in SAP ByD
		'''
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khsupplierinvoice/SupplierInvoiceCollection"
		calculate_gross = f"{self.endpoint}/sap/byd/odata/cust/v1/khsupplierinvoice/CalculateGrossAmount?ObjectID="
		calculate_tax = f"{self.endpoint}/sap/byd/odata/cust/v1/khsupplierinvoice/CalculateTaxAmount?ObjectID="
		try:
			self.refresh_csrf_token()
			# Limit invoice description to 40 chars per ByD's rule
			invoice_data["InvoiceDescription"] = invoice_data["InvoiceDescription"][:40] or "Inv Frm eGRN Sys"
			
			response = self.__post__(action_url, json=invoice_data)
			if response.status_code == 201:
				response_data = response.json()
				logger.info(f"Invoice successfully created in SAP ByD.")
				object_id = response_data.get("d", {}).get("results", {}).get("ObjectID")
				
				# Add a small delay before calculations
				time.sleep(2)
				
				# Calculate gross amount
				gross_url = f"{calculate_gross}'{object_id}'"
				gross_response = self.__post__(gross_url)
				if gross_response.status_code == 200:
					# Add a small delay before tax calculation
					time.sleep(2)
					# Calculate tax amount
					tax_url = f"{calculate_tax}'{object_id}'"
					tax_response = self.__post__(tax_url)
					if tax_response.status_code != 200:
						logger.error(f"Failed to calculate tax amount: {tax_response.text}")
						raise Exception(f"Error from SAP: {tax_response.text}")
				else:
					logger.error(f"Failed to calculate gross amount: {gross_response.text}")
					raise Exception(f"Error from SAP: {gross_response.text}")
					
				return response.json()
			else:
				logger.error(f"Failed to create Invoice: {response.text}")
				raise Exception(f"{response.text}")
		except Exception as e:
			logger.error(f"Error creating Invoice: {str(e)}")
			raise
			
	def post_invoice(self, object_id: str) -> dict:
		'''
			Post a Supplier Invoice in SAP ByD
		'''
		# Check if object is locked
		if self.check_object_lock(object_id, 'invoice'):
			logger.warning(f"Invoice {object_id} is locked. Will retry later.")
			raise Exception("Object is locked")
			
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khsupplierinvoice/FinishDataEntryProcessing?ObjectID='{object_id}'"
		try:
			self.refresh_csrf_token()
			response = self.__post__(action_url)
			if response.status_code == 200:
				logger.info(f"Invoice successfully POSTED.")
				return response.json()
			else:
				logger.error(f"Failed to post Invoice: {response.text}")
				raise Exception(f"Error from SAP: {response.text}")
		except Exception as e:
			logger.error(f"Error posting Invoice: {str(e)}")
			raise
	
	def create_inbound_delivery_notification(self, delivery_data: dict) -> dict:
		'''
			Create an Inbound Delivery Notification in SAP ByD
		'''
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khinbounddelivery/InboundDeliveryCollection"
		try:
			self.refresh_csrf_token()
			response = self.__post__(action_url, json=delivery_data)
			if response.status_code == 201:
				logger.info(f"Delivery Notification successfully created in SAP ByD.")
				return response.json()
			else:
				logger.error(f"Failed to create Delivery Notification: {response.text}")
				raise Exception(f"Error from SAP: {response.text}")
		except Exception as e:
			logger.error(f"Error creating Delivery Notification: {str(e)}")
			raise
	
	def post_delivery_notification(self, object_id: str) -> dict:
		'''
			Post an Inbound Delivery Notification in SAP ByD
		'''
		# Check if object is locked
		if self.check_object_lock(object_id, 'delivery'):
			logger.warning(f"Delivery Notification {object_id} is locked. Will retry later.")
			raise Exception("Object is locked")
			
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khinbounddelivery/PostGoodsReceipt?ObjectID='{object_id}'"
		try:
			self.refresh_csrf_token()
			response = self.__post__(action_url)
			if response.status_code == 200:
				logger.info(f"Delivery Notification successfully POSTED.")
				return response.json()
			else:
				logger.error(f"Failed to post Delivery Notification: {response.text}")
				raise Exception(f"Error from SAP: {response.text}")
		except Exception as e:
			logger.error(f"Error posting Delivery Notification: {str(e)}")
			raise
	
	# Sales Order methods for store-to-store transfers
	def get_sales_order_by_id(self, sales_order_id: str) -> dict:
		'''
			Fetch a sales order from SAP ByD by ID
		'''
		action_url = (f"{self.endpoint}/sap/byd/odata/cust/v1/khsalesorder/SalesOrderCollection?$format=json"
					  f"&$expand=BuyerParty/BuyerPartyName,SalesUnitParty/SalesUnitPartyName,ProductRecipientParty,"
					  f"RequestedFulfillmentPeriod,PricingTerms,Item/ItemProduct,Item/ItemScheduleLine,"
					  f"Item/ItemShipFromLocation,Item/ItemProductRecipientParty,Item&$filter=ID eq '{sales_order_id}'")
		
		try:
			response = self.__get__(action_url)
			if response.status_code == 200:
				response_json = json.loads(response.text)
				results = response_json["d"]["results"]
				return results[0] if results else None
			else:
				logger.error(f"Failed to fetch sales order {sales_order_id}: {response.text}")
				return None
		except Exception as e:
			logger.error(f"Error fetching sales order {sales_order_id}: {str(e)}")
			raise
	
	def get_sales_order_outbound_deliveries(self, sales_order_id: str) -> list:
		'''
			List the outbound deliveries ByD has created for a sales order.

			Verified against khsalesorder (tenant my350679, Sept 2026): the sales
			order's DocumentReference node lists successor documents; outbound
			deliveries carry TypeCode "73". Only deliveries appear there - the
			intermediate delivery request (TypeCode 68) is never referenced.
			ByD ObjectIDs are the UUID with the dashes removed, so the delivery
			ObjectID (needed by khoutbounddelivery/Release) is derived here.

			Returns [{"ID": "11", "UUID": "...", "ObjectID": "..."}], empty when
			no delivery exists yet.
		'''
		action_url = (f"{self.endpoint}/sap/byd/odata/cust/v1/khsalesorder/SalesOrderCollection?$format=json"
					  f"&$expand=DocumentReference&$filter=ID eq '{sales_order_id}'")
		try:
			response = self.__get__(action_url)
			if response.status_code != 200:
				logger.error(f"Failed to fetch document references for sales order {sales_order_id}: {response.text}")
				return []
			results = json.loads(response.text)["d"]["results"]
			if not results:
				return []
			references = results[0].get("DocumentReference") or []
			if isinstance(references, dict):
				# {"results": [...]} or a {"__deferred": ...} stub when empty
				references = references.get("results") or []
			deliveries = []
			for ref in references:
				if str(ref.get("TypeCode")) != "73" or not ref.get("UUID"):
					continue
				deliveries.append({
					"ID": ref.get("ID"),
					"UUID": ref["UUID"],
					"ObjectID": ref["UUID"].replace("-", "").upper(),
				})
			return deliveries
		except Exception as e:
			logger.error(f"Error fetching document references for sales order {sales_order_id}: {str(e)}")
			raise

	def get_store_sales_orders(self, store_id: str) -> list:
		'''
			Get sales orders for a specific store (as source or destination)
		'''
		# This assumes store_id maps to a cost center or similar identifier in SAP ByD
		action_url = (f"{self.endpoint}/sap/byd/odata/cust/v1/khsalesorder/SalesOrderCollection?$format=json"
					  f"&$expand=Item,BuyerParty,SellerParty"
					  f"&$filter=BuyerParty/PartyID eq '{store_id}' or SellerParty/PartyID eq '{store_id}'")
		
		try:
			response = self.__get__(action_url)
			if response.status_code == 200:
				response_json = json.loads(response.text)
				return response_json["d"]["results"]
			else:
				logger.error(f"Failed to fetch sales orders for store {store_id}: {response.text}")
				return []
		except Exception as e:
			logger.error(f"Error fetching sales orders for store {store_id}: {str(e)}")
			raise

	def get_product_details(self, material_id: str) -> dict:
		'''
			Fetch product/material details from SAP ByD by InternalID
		'''
		action_url = (
			f"{self.endpoint}/sap/byd/odata/cust/v1/vmumaterial/MaterialCollection?"
			f"$filter=InternalID eq '{material_id}'"
			f"&$format=json&sap-language=EN"
			f"&$select=InternalID,Description,DescriptionLanguageCode,DescriptionLanguageCodeText,"
			f"BaseMeasureUnitCode,BaseMeasureUnitCodeText,IdentifiedStockTypeCode,IdentifiedStockTypeCodeText"
		)
		
		try:
			response = self.session.get(action_url, auth=self.comm_auth)
			if response.status_code == 200:
				response_json = json.loads(response.text)
				results = response_json["d"]["results"]
				return results[0] if results else None
			else:
				logger.error(f"Failed to fetch product {material_id}: {response.text}")
				return None
		except Exception as e:
			logger.error(f"Error fetching product {material_id}: {str(e)}")
			raise
	
	def get_location_by_id(self, location_id: str) -> dict:
		'''
			Fetch location master data from SAP ByD by ID. Cached for 24h since warehouse
			details change rarely and the same locations are looked up repeatedly.
		'''
		if not location_id:
			return None

		cache_key = f"byd:location:{location_id}"
		cached = cache.get(cache_key)
		if cached is not None:
			return cached

		action_url = (
			f"{self.endpoint}/sap/byd/odata/cust/v1/khlocation/LocationCollection?"
			f"$format=json&$filter=ID eq '{location_id}'&$top=1"
		)
		try:
			response = self.session.get(action_url, auth=self.comm_auth)
			if response.status_code == 200:
				results = json.loads(response.text)["d"]["results"]
				location = results[0] if results else None
				if location:
					cache.set(cache_key, location, 60 * 60 * 24)
				return location
			logger.error(f"Failed to fetch location {location_id}: {response.text}")
			return None
		except Exception as e:
			logger.error(f"Error fetching location {location_id}: {str(e)}")
			raise

	def _enrich_delivery_items(self, delivery: dict) -> dict:
		'''
			Attach product details and material-valuation pricing to each line item
			of an outbound-delivery dict returned by ByD.
		'''
		if not delivery or "Item" not in delivery:
			return delivery
		items = delivery["Item"]
		if isinstance(items, dict):
			items = items.get("results", [])
		if isinstance(items, list):
			for item in items:
				material_id = item.get("ProductID") or item.get("ItemProduct", {}).get("ProductID")
				if not material_id:
					continue
				product_details = self.get_product_details(material_id)
				if product_details:
					item.update(product_details)

				# Get pricing from material valuation
				material_valuation = self.get_material_valuation(material_id)
				if material_valuation:
					item["unit_price"] = material_valuation.get("unit_price", 0)
					item["currency_code"] = material_valuation.get("currency_code", "NGN")
					item["valuation_date"] = material_valuation.get("valuation_date")
			delivery["Item"] = items
		return delivery

	def get_delivery_by_id(self, delivery_id: str) -> dict:
		'''
			Fetch an outbound delivery (warehouse-to-store) from SAP ByD by ID
		'''
		action_url = (f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddelivery/OutboundDeliveryCollection?$format=json"
					  f"&$expand=Item/ItemDeliveryQuantity,ProductRecipientParty/ProductRecipientDisplayName,"
					  f"ShipFromLocation,ShippingPeriod,ArrivalPeriod"
					  f"&$filter=ID eq '{delivery_id}'")
		try:
			response = self.__get__(action_url)
			if response.status_code == 200:
				response_json = json.loads(response.text)
				results = response_json["d"]["results"]
				delivery = results[0] if results else None
				return self._enrich_delivery_items(delivery)
			else:
				logger.error(f"Failed to fetch delivery {delivery_id}: {response.text}")
				return None
		except Exception as e:
			logger.error(f"Error fetching delivery {delivery_id}: {str(e)}")
			raise

	class DeliveryRequestNotFound(Exception):
		"""No delivery request in ByD matches the sales order."""

	class DeliveryRequestAmbiguous(Exception):
		"""More than one delivery request matches the sales order."""

	@staticmethod
	def _byd_epoch_ms(value):
		"""'/Date(1747737892692)/' -> 1747737892692; ISO strings -> epoch ms; else None."""
		if not value:
			return None
		text = str(value)
		match = re.search(r"/Date\((-?\d+)", text)
		if match:
			return int(match.group(1))
		try:
			from datetime import datetime, timezone as dt_tz
			parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
			if parsed.tzinfo is None:
				parsed = parsed.replace(tzinfo=dt_tz.utc)
			return int(parsed.timestamp() * 1000)
		except ValueError:
			return None

	@staticmethod
	def _sales_order_fingerprint(sales_order: dict) -> dict:
		"""
			{item ID: (ProductID, requested quantity)} for the non-cancelled items of a
			khsalesorder payload. The delivery request repeats the sales order item
			number as BaseBusinessTransactionDocumentItemID, so this is the join key.
		"""
		fingerprint = {}
		items = sales_order.get("Item") or []
		if isinstance(items, dict):
			items = items.get("results") or []
		for item in items:
			if str(item.get("CancellationStatusCode", "")) == "4":
				continue
			product = item.get("ItemProduct") or {}
			product_id = product.get("ProductID") if isinstance(product, dict) else None
			lines = item.get("ItemScheduleLine") or []
			if isinstance(lines, dict):
				lines = lines.get("results") or []
			requested = next((l for l in lines if str(l.get("TypeCode")) == "1"), lines[0] if lines else {})
			quantity = requested.get("Quantity")
			if item.get("ID") and product_id and quantity is not None:
				fingerprint[str(item["ID"])] = (product_id, round(float(quantity), 3))
		return fingerprint

	@staticmethod
	def _delivery_request_fingerprint(request: dict) -> dict:
		"""Same shape as _sales_order_fingerprint, built from a khoutbounddeliveryrequest payload."""
		fingerprint = {}
		items = request.get("Item") or []
		if isinstance(items, dict):
			items = items.get("results") or []
		for item in items:
			if str(item.get("CancellationStatusCode", "")) == "4":
				continue
			lines = item.get("ItemScheduleLine") or []
			if isinstance(lines, dict):
				lines = lines.get("results") or []
			quantity = None
			for line in lines:
				requested = line.get("RequestedQuantity") or {}
				if isinstance(requested, dict) and requested.get("Quantity") is not None:
					quantity = float(requested["Quantity"])
					break
			item_id = item.get("BaseBusinessTransactionDocumentItemID")
			if item_id and item.get("ProductID") and quantity is not None:
				fingerprint[str(item_id)] = (item["ProductID"], round(quantity, 3))
		return fingerprint

	def query_delivery_requests_by_sales_order(self, sales_order_id: str) -> list:
		"""
			khoutbounddeliveryrequest/QueryByElements?SalesOrderID='...' - the service's
			function import exposes the sales-order key that the entity itself lacks
			($metadata, Sept 2026). Returns the matching requests with items expanded.
		"""
		action_url = (
			f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddeliveryrequest/QueryByElements"
			f"?SalesOrderID='{sales_order_id}'&$format=json"
			f"&$expand=Item/ItemBuyerParty,Item/ItemScheduleLine/RequestedQuantity,Item/ItemScheduleLine/OpenQuantity"
		)
		response = self.__get__(action_url)
		if response.status_code != 200:
			logger.error(f"QueryByElements failed for sales order {sales_order_id}: {response.text}")
			raise Exception(f"Error from SAP: {response.text}")
		results = json.loads(response.text)["d"]["results"]
		return results if isinstance(results, list) else [results]

	def find_delivery_request_for_sales_order(self, sales_order: dict, window_hours: int = 3) -> dict:
		"""
			Locate the ByD Outbound Delivery Request created for a sales order.

			Primary: QueryByElements?SalesOrderID (see query_delivery_requests_by_sales_order),
			verified against the item fingerprint below. Fallback, if that call fails:
			scan requests created around the order's release and match on content -
			the request's items repeat the sales order item number
			(BaseBusinessTransactionDocumentItemID), ProductID and requested quantity.
			Requests are created when the order is *released*, so the window spans
			CreationDateTime..LastChangeDateTime plus a margin; several orders are
			routinely released within minutes, hence content matching.

			Returns the matching request (items expanded). Raises
			DeliveryRequestNotFound / DeliveryRequestAmbiguous rather than guessing.
		"""
		so_id = sales_order.get("ID")
		wanted = self._sales_order_fingerprint(sales_order)
		if not wanted:
			raise self.DeliveryRequestNotFound(f"Sales order {so_id} has no deliverable items to match on")

		try:
			by_key = self.query_delivery_requests_by_sales_order(so_id)
		except Exception as e:
			logger.warning(f"QueryByElements unavailable for sales order {so_id} ({e}); falling back to content match")
			by_key = None
		if by_key is not None:
			if not by_key:
				raise self.DeliveryRequestNotFound(f"No delivery request is linked to sales order {so_id}")
			exact = [r for r in by_key if self._delivery_request_fingerprint(r) == wanted]
			chosen = exact if exact else by_key
			if len(chosen) > 1:
				ids = ", ".join(str(r.get("BaseBusinessTransactionDocumentID")) for r in chosen)
				raise self.DeliveryRequestAmbiguous(f"Delivery requests {ids} are all linked to sales order {so_id}")
			if not exact:
				logger.warning(
					f"Delivery request {chosen[0].get('BaseBusinessTransactionDocumentID')} is linked to sales order "
					f"{so_id} but its items differ from the order (order changed after release?)"
				)
			return chosen[0]

		from datetime import datetime, timedelta, timezone as dt_tz

		created = self._byd_epoch_ms(sales_order.get("CreationDateTime"))
		changed = self._byd_epoch_ms(sales_order.get("LastChangeDateTime")) or created
		if created is None:
			raise self.DeliveryRequestNotFound(f"Sales order {so_id} has no CreationDateTime")
		margin = timedelta(hours=window_hours)
		low = datetime.fromtimestamp(created / 1000, dt_tz.utc) - margin
		high = datetime.fromtimestamp(max(created, changed) / 1000, dt_tz.utc) + margin
		fmt = "%Y-%m-%dT%H:%M:%SZ"

		# CreationDateTime is Edm.DateTimeOffset on this entity: the literal must be
		# datetimeoffset'...' (datetime'...' is rejected with "Invalid parametertype").
		action_url = (
			f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddeliveryrequest/OutboundDeliveryRequestCollection"
			f"?$format=json&$top=200"
			f"&$filter=CreationDateTime ge datetimeoffset'{low.strftime(fmt)}' and "
			f"CreationDateTime le datetimeoffset'{high.strftime(fmt)}'"
			f"&$expand=Item/ItemBuyerParty,Item/ItemScheduleLine/RequestedQuantity,Item/ItemScheduleLine/OpenQuantity"
		)
		response = self.__get__(action_url)
		if response.status_code != 200:
			logger.error(f"Failed to list delivery requests for sales order {so_id}: {response.text}")
			raise Exception(f"Error from SAP: {response.text}")
		candidates = json.loads(response.text)["d"]["results"]

		matches = [c for c in candidates if self._delivery_request_fingerprint(c) == wanted]
		if not matches:
			raise self.DeliveryRequestNotFound(
				f"No delivery request among {len(candidates)} candidates matches sales order {so_id}"
			)
		if len(matches) > 1:
			ids = ", ".join(str(m.get("BaseBusinessTransactionDocumentID")) for m in matches)
			raise self.DeliveryRequestAmbiguous(
				f"Delivery requests {ids} all match sales order {so_id}; refusing to post"
			)
		logger.info(
			f"Sales order {so_id} matched delivery request "
			f"{matches[0].get('BaseBusinessTransactionDocumentID')} ({matches[0].get('ObjectID')})"
		)
		return matches[0]

	def post_goods_issue_for_request_item(self, item_object_id: str, auto_release: bool = True) -> dict:
		"""
			Create the outbound delivery for one delivery-request item and post its
			goods issue: POST khoutbounddeliveryrequest/ItemPostGoodsIssue (from the
			Postman collection "Post goods issue"). With AutoReleaseOutboundDelivery
			the delivery is released in the same call; without it the delivery is left
			"Not Released" for release_outbound_delivery().

			The action takes no quantity: ByD ships the item's full open quantity.
			THIS MOVES STOCK. Callers gate it behind settings.BYD_AUTO_POST_GOODS_ISSUE.
		"""
		flag = "true" if auto_release else "false"
		action_url = (
			f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddeliveryrequest/ItemPostGoodsIssue"
			f"?ObjectID='{item_object_id}'&AutoReleaseOutboundDelivery={flag}"
		)
		self.refresh_csrf_token()
		response = self.__post__(action_url)
		if response.status_code in (200, 201, 204):
			logger.info(f"Goods issue posted for delivery request item {item_object_id}")
			return response.json() if response.text else {}
		logger.error(f"Failed to post goods issue for request item {item_object_id}: {response.text}")
		raise Exception(f"Error from SAP: {response.text}")

	def release_outbound_delivery(self, delivery_object_id: str) -> dict:
		"""
			Release an existing "Not Released" outbound delivery:
			POST khoutbounddelivery/Release?ObjectID='...' (Postman "Release outbound delivery").
			THIS MOVES STOCK. Callers gate it behind settings.BYD_AUTO_POST_GOODS_ISSUE.
		"""
		if self.check_object_lock(delivery_object_id, 'delivery'):
			logger.warning(f"Outbound delivery {delivery_object_id} is locked. Will retry later.")
			raise Exception("Object is locked")
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddelivery/Release?ObjectID='{delivery_object_id}'"
		self.refresh_csrf_token()
		response = self.__post__(action_url)
		if response.status_code in (200, 201, 204):
			logger.info(f"Outbound delivery {delivery_object_id} released")
			return response.json() if response.text else {}
		logger.error(f"Failed to release outbound delivery {delivery_object_id}: {response.text}")
		raise Exception(f"Error from SAP: {response.text}")

	def get_outbound_delivery_by_object_id(self, object_id: str) -> dict:
		"""Key access on khoutbounddelivery; d.results is a single object here, not a list."""
		action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddelivery/OutboundDeliveryCollection('{object_id}')?$format=json"
		response = self.__get__(action_url)
		if response.status_code != 200:
			logger.error(f"Failed to fetch outbound delivery {object_id}: {response.text}")
			return None
		result = json.loads(response.text)["d"]["results"]
		return result[0] if isinstance(result, list) else result

	def search_deliveries_by_store(self, store_id: str, status: str = None) -> list:
		'''
			Search for outbound deliveries (warehouse-to-store) assigned to a specific store
		'''
		action_url = (f"{self.endpoint}/sap/byd/odata/cust/v1/khoutbounddelivery/OutboundDeliveryCollection?$format=json"
					  f"&$expand=Item/ItemDeliveryQuantity,ProductRecipientParty/ProductRecipientDisplayName,"
					  f"ShipFromLocation,ShippingPeriod,ArrivalPeriod"
					  f"&$filter=ProductRecipientParty/PartyID eq '{store_id}' and DeliveryTypeCode eq 'STOD'")
		
		if status:
			action_url += f" and DeliveryProcessingStatusCode eq '{status}'"
		
		try:
			response = self.__get__(action_url)
			if response.status_code == 200:
				response_json = json.loads(response.text)
				return response_json["d"]["results"]
			else:
				logger.error(f"Failed to fetch deliveries for store {store_id}: {response.text}")
				return []
		except Exception as e:
			logger.error(f"Error fetching deliveries for store {store_id}: {str(e)}")
			raise

	def complete_transfer_in_sap(self, sales_order_id: str, goods_issue_object_id: str = None) -> dict:
		'''
			Complete a store-to-store transfer in SAP ByD by updating sales order status
			and linking goods issue document
		'''
		try:
			# First get the sales order to get its ObjectID
			sales_order = self.get_sales_order_by_id(sales_order_id)
			if not sales_order:
				logger.error(f"Sales order {sales_order_id} not found")
				raise Exception(f"Sales order {sales_order_id} not found")
			
			object_id = sales_order.get("ObjectID")
			if not object_id:
				logger.error(f"ObjectID not found for sales order {sales_order_id}")
				raise Exception(f"ObjectID not found for sales order {sales_order_id}")
			
			# Update the delivery status to completely delivered
			action_url = f"{self.endpoint}/sap/byd/odata/cust/v1/khsalesorder/SalesOrderCollection('{object_id}')"
			update_data = {
				"DeliveryStatusCode": "3"  # Completely Delivered
			}
			
			# Add goods issue document reference if provided
			if goods_issue_object_id:
				update_data["GoodsIssueReference"] = goods_issue_object_id
			
			self.refresh_csrf_token()
			response = self.session.patch(action_url, json=update_data, headers=self.auth_headers, auth=self.auth)
			
			if response.status_code == 204:  # PATCH typically returns 204 No Content on success
				logger.info(f"Transfer completed for sales order {sales_order_id}")
				return {
					"success": True,
					"sales_order_id": sales_order_id,
					"object_id": object_id,
					"status": "Completely Delivered",
					"goods_issue_reference": goods_issue_object_id
				}
			else:
				logger.error(f"Failed to complete transfer: {response.text}")
				raise Exception(f"Error from SAP: {response.text}")
				
		except Exception as e:
			logger.error(f"Error completing transfer for sales order {sales_order_id}: {str(e)}")
			raise

	def get_material_valuation(self, material_id: str) -> dict:
		'''
		Fetch material valuation/standard cost from SAP ByD.
		Uses the MaterialValuationDataCollection endpoint with ValuationPrice expansion.

		Args:
			material_id: The material/product ID to fetch valuation for

		Returns:
			dict with unit_price, currency_code, and valuation_date, or None if not found
		'''
		from datetime import datetime

		# Use MaterialValuationDataCollection endpoint with ValuationPrice expanded
		action_url = (
			f"{self.endpoint}/sap/byd/odata/cust/v1/vmumaterialvaluationdata/"
			f"MaterialValuationDataCollection?$format=json"
			f"&$filter=MateriallID eq '{material_id}'"
			f"&$expand=ValuationPrice"
		)

		try:
			response = self.session.get(action_url, auth=self.comm_auth)
			if response.status_code == 200:
				response_json = json.loads(response.text)
				results = response_json.get("d", {}).get("results", [])

				if not results:
					logger.warning(f"No material valuation found for material {material_id}")
					return None

				# Get the first valuation record
				valuation_record = results[0]

				# Check if ValuationPrice is expanded
				valuation_prices = valuation_record.get("ValuationPrice", {})
				if isinstance(valuation_prices, dict) and "results" in valuation_prices:
					price_records = valuation_prices["results"]
				elif isinstance(valuation_prices, list):
					price_records = valuation_prices
				else:
					logger.warning(f"No ValuationPrice data for material {material_id}")
					return None

				if not price_records:
					logger.warning(f"Empty ValuationPrice array for material {material_id}")
					return None

				# Helper function to parse SAP ByD date format: /Date(milliseconds)/
				def parse_sap_date(date_string):
					if not date_string:
						return None
					try:
						# Extract milliseconds from /Date(1234567890000)/
						if isinstance(date_string, str) and date_string.startswith("/Date("):
							ms = int(date_string.replace("/Date(", "").replace(")/", ""))
							return datetime.fromtimestamp(ms / 1000.0)
					except Exception as e:
						logger.warning(f"Failed to parse date {date_string}: {e}")
					return None

				# Filter for valid prices with date ranges
				now = datetime.now()
				valid_prices = []

				for price_record in price_records:
					# Get TypeCode - it might be in different formats
					type_code = price_record.get("TypeCode") or price_record.get("TypeCode_content")

					# Check if price type is inventory cost (TypeCode == "1")
					# Accept if TypeCode is "1" or if TypeCode is missing (less strict)
					if type_code and type_code != "1":
						continue

					start_date = parse_sap_date(price_record.get("StartDate"))
					end_date = parse_sap_date(price_record.get("EndDate"))

					# Check if price is currently valid
					is_valid = True
					if start_date and start_date > now:
						is_valid = False
					if end_date and end_date < now:
						is_valid = False

					if is_valid:
						# Get amount - can be string, number, or dict
						amount = price_record.get("Amount")
						currency = price_record.get("AmountCurrencyCode", "NGN")

						try:
							if isinstance(amount, str):
								price_value = float(amount)
							elif isinstance(amount, dict):
								price_value = float(amount.get("content", 0))
								currency = amount.get("currencyCode", currency)
							elif isinstance(amount, (int, float)):
								price_value = float(amount)
							else:
								price_value = 0.0
						except (ValueError, TypeError):
							price_value = 0.0

						if price_value > 0:
							valid_prices.append({
								"unit_price": price_value,
								"currency_code": currency,
								"start_date": start_date,
								"end_date": end_date,
								"valuation_date": start_date.isoformat() if start_date else None
							})

				if not valid_prices:
					logger.warning(f"No valid price records found for material {material_id}")
					return None

				# Return the price with the most recent start_date
				valid_prices.sort(key=lambda x: x["start_date"] or datetime.min, reverse=True)
				selected_price = valid_prices[0]

				return {
					"unit_price": selected_price["unit_price"],
					"currency_code": selected_price["currency_code"],
					"valuation_date": selected_price["valuation_date"]
				}

			else:
				logger.error(f"Failed to fetch material valuation for {material_id}: {response.text}")
				return None
		except Exception as e:
			logger.error(f"Error fetching material valuation for {material_id}: {str(e)}")
			raise
