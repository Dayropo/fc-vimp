import logging
import os
import uuid
from datetime import date

import pandas as pd
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q, Prefetch
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes

from byd_service.rest import RESTServices
from overrides.authenticate import CombinedAuthentication
from overrides.rest_framework import APIResponse, CustomPagination
from .models import StoreAuthorization, InboundDelivery, TransferReceiptNote
from .serializers import (
	TransferReceiptNoteSerializer, InboundDeliverySerializer
)
from .services import AuthorizationService

logger = logging.getLogger(__name__)

EXPORT_DOWNLOAD_DIR = 'downloads'
EXPORT_FILENAME_PREFIX = 'transfers_search'
EXPORT_COLUMNS = [
	'Sales Order Ref',
	'GTN No (Delivery ID)',
	'Receipt ID',
	'Source Location ID',
	'Source Location Name',
	'Destination Store Name',
	'Destination Store Code',
	'Delivery Date',
	'Delivery Status Code',
	'Produce Name',
	'Product Code',
	'UoM',
	'Unit Price',
	'Expected Qty',
	'Qty Received',
	'Outstanding Qty',
	'Confirmation Status',
	'Delivery Status',
	'Delivery Type Code',
	'Created Date',
]


def _format_date(value):
	return value.isoformat() if value else ''


def _build_export_rows(queryset):
	queryset = queryset.prefetch_related(
		'line_items',
		Prefetch('receipts', queryset=TransferReceiptNote.objects.order_by('-created_date')),
	).select_related('destination_store')

	rows = []
	for delivery in queryset:
		dest_store = getattr(delivery, 'destination_store', None)
		receipts = list(delivery.receipts.all())
		latest_receipt = receipts[0] if receipts else None

		delivery_base = {
			'Sales Order Ref':        delivery.sales_order_reference or '',
			'GTN No (Delivery ID)':   delivery.delivery_id,
			'Receipt ID':             latest_receipt.receipt_number if latest_receipt else '',
			'Source Location ID':     delivery.source_location_id,
			'Source Location Name':   delivery.source_location_name,
			'Destination Store Name': getattr(dest_store, 'store_name', ''),
			'Destination Store Code': getattr(dest_store, 'byd_cost_center_code', ''),
			'Delivery Date':          _format_date(delivery.delivery_date),
			'Delivery Status Code':   delivery.delivery_status_code,
			'Confirmation Status':    latest_receipt.get_approval_status_display() if latest_receipt else '',
			'Delivery Status':        delivery.delivery_status,
			'Delivery Type Code':     delivery.delivery_type_code,
			'Created Date':           _format_date(delivery.created_date),
		}

		line_items = list(delivery.line_items.all())
		if line_items:
			for li in line_items:
				rows.append({
					**delivery_base,
					'Produce Name':    li.product_name,
					'Product Code':    li.product_id,
					'UoM':             li.unit_of_measurement,
					'Unit Price':      float(li.unit_price) if li.unit_price else '',
					'Expected Qty':    float(li.quantity_expected),
					'Qty Received':    float(li.quantity_received),
					'Outstanding Qty': float(li.quantity_outstanding),
				})
		else:
			rows.append({
				**delivery_base,
				'Produce Name': '', 'Product Code': '', 'UoM': '',
				'Unit Price': '', 'Expected Qty': '', 'Qty Received': '', 'Outstanding Qty': '',
			})
	return rows


def _write_export_file(rows):
	download_root = os.path.join(settings.MEDIA_ROOT, EXPORT_DOWNLOAD_DIR)
	os.makedirs(download_root, exist_ok=True)
	timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
	filename = f"{EXPORT_FILENAME_PREFIX}_{timestamp}_{uuid.uuid4().hex[:8]}.xlsx"
	filepath = os.path.join(download_root, filename)

	df = pd.DataFrame(rows)
	if df.empty:
		df = pd.DataFrame(columns=EXPORT_COLUMNS)
	else:
		df = df.reindex(columns=EXPORT_COLUMNS)

	try:
		df.to_excel(filepath, index=False)
	except Exception as exc:
		logger.error(f"Failed to write delivery export to Excel: {exc}")
		raise

	return filename


def _normalize_media_url():
	media_url = settings.MEDIA_URL or 'media/'
	if not media_url.startswith('/'):
		media_url = f"/{media_url}"
	return media_url.rstrip('/')


def _generate_search_export_link(queryset, request):
	rows = _build_export_rows(queryset)
	filename = _write_export_file(rows)
	download_path = f"{_normalize_media_url()}/{EXPORT_DOWNLOAD_DIR}/{filename}"
	return request.build_absolute_uri(download_path)


def _apply_delivery_filters(queryset, query_params):
	"""
		Apply optional delivery filters based on request query parameters
	"""
	delivery_id = query_params.get('delivery_id')
	source_location_id = query_params.get('source_location_id')
	source_location_name = query_params.get('source_location_name')
	destination_store = query_params.get('destination_store')
	delivery_date = query_params.get('delivery_date')
	delivery_date_from = query_params.get('delivery_date_from')
	delivery_date_to = query_params.get('delivery_date_to')
	delivery_status_code = query_params.get('delivery_status_code') or query_params.get('status')
	delivery_type_code = query_params.get('delivery_type_code')
	sales_order_reference = query_params.get('sales_order_reference')

	if delivery_id:
		queryset = queryset.filter(delivery_id__icontains=delivery_id)
	if source_location_id:
		queryset = queryset.filter(source_location_id__icontains=source_location_id)
	if source_location_name:
		queryset = queryset.filter(source_location_name__icontains=source_location_name)
	if destination_store:
		queryset = queryset.filter(
			Q(destination_store__store_name__icontains=destination_store) |
			Q(destination_store__byd_cost_center_code__icontains=destination_store)
		)
	if delivery_date:
		try:
			parsed_date = date.fromisoformat(delivery_date)
		except ValueError:
			raise ValidationError("delivery_date must be in YYYY-MM-DD format")
		queryset = queryset.filter(delivery_date=parsed_date)
	if delivery_date_from:
		try:
			parsed_from = date.fromisoformat(delivery_date_from)
		except ValueError:
			raise ValidationError("delivery_date_from must be in YYYY-MM-DD format")
		queryset = queryset.filter(delivery_date__gte=parsed_from)
	if delivery_date_to:
		try:
			parsed_to = date.fromisoformat(delivery_date_to)
		except ValueError:
			raise ValidationError("delivery_date_to must be in YYYY-MM-DD format")
		queryset = queryset.filter(delivery_date__lte=parsed_to)
	if delivery_type_code:
		queryset = queryset.filter(delivery_type_code__icontains=delivery_type_code)
	if sales_order_reference:
		queryset = queryset.filter(sales_order_reference__icontains=sales_order_reference)
	if delivery_status_code:
		queryset = queryset.filter(delivery_status_code=delivery_status_code)

	return queryset

# Create your views here.

@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_inbound_deliveries(request):
	"""
		List inbound deliveries for the authenticated user's authorized stores
		Supports searching by delivery ID and pagination
	"""
	try:
		user = request.user
		# Visible deliveries = destination StoreAuthorization (Restaurant Manager)
		# UNION source-location auth (SCD); Finance bypasses entirely.
		queryset = AuthorizationService.filter_deliveries_for_user(
			user, InboundDelivery.objects.all()
		)

		try:
			queryset = _apply_delivery_filters(queryset, request.query_params)
		except ValidationError as e:
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message=str(e)
			)
			
		# Order queryset
		queryset = queryset.order_by('-created_date')
		
		# Apply pagination
		paginator = CustomPagination()
		paginated_queryset = paginator.paginate_queryset(queryset, request)
		serializer = InboundDeliverySerializer(paginated_queryset, many=True)
		
		return APIResponse(
			status=status.HTTP_200_OK,
			message='Inbound deliveries fetched successfully',
			data=paginator.get_paginated_response(serializer.data).data
		)
	except Exception as e:
		logger.error(f"Error fetching inbound deliveries: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message='Internal server error while fetching inbound deliveries'
		)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_inbound_delivery(request, pk):
	"""
	Get the expected receipt for a Sales Order so the store can record what it
	actually received.

	`pk` is the SAP ByD Sales Order ID. This mirrors egrn_service.get_purchase_order's
	single-identifier, local-then-ByD pattern - `pk` here plays the role `po_id`
	plays there. No Outbound Delivery is read or required: under the post-cutover
	flow the outbound delivery is only created in ByD once SCD approves the receipt.

	Query Parameters:
	- refresh: If 'true', discard the cached copy and re-read from SAP ByD
	"""
	try:
		# Check if refresh is requested
		refresh = request.query_params.get('refresh', '').lower() == 'true'

		delivery = (
			InboundDelivery.objects
			.filter(sales_order_reference=pk)
			.order_by('-delivery_date', '-id')
			.first()
		)

		if refresh and delivery is not None and delivery.receipts.exists():
			# Re-reading deletes and recreates the row, and TransferReceiptNote
			# cascades off it - refuse rather than destroy receipt history.
			return APIResponse(
				status=status.HTTP_409_CONFLICT,
				message=(
					f"Sales order {pk} already has receipts recorded against it and "
					f"cannot be refreshed from SAP ByD."
				)
			)

		# If refresh requested or nothing cached, read the sales order from SAP ByD
		if refresh or delivery is None:
			byd_rest = RESTServices()

			sales_order_data = byd_rest.get_sales_order_by_id(pk)
			if not sales_order_data:
				return APIResponse(
					status=status.HTTP_404_NOT_FOUND,
					message=f"Sales order {pk} not found in SAP ByD"
				)

			# Attach product details and pricing to each item for the receiving screen
			sales_order_data = byd_rest._enrich_delivery_items(sales_order_data)

			# Drop the stale cached copy before recreating it
			if delivery:
				delivery.line_items.all().delete()
				delivery.delete()

			delivery = InboundDelivery.create_from_sales_order_data(sales_order_data)

		# Authorized if user can access via destination store OR source warehouse
		if not AuthorizationService.user_can_access_delivery(request.user, delivery):
			return APIResponse(
				status=status.HTTP_403_FORBIDDEN,
				message="You are not authorized to access this delivery"
			)
		# Return the expected-receipt data
		return APIResponse(
			status=status.HTTP_200_OK,
			message=(
				f"Sales order {pk} fetched successfully" if not refresh
				else f"Sales order {pk} refreshed successfully"
			),
			data=InboundDeliverySerializer(delivery).data
		)
	except Exception as e:
		logger.error(f"Error fetching sales order {pk}: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message=f"Error fetching sales order {pk}: {e}"
		)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def search_deliveries(request):
	"""
		Search in the local database for deliveries using any of the following parameters:
			delivery_id
			source_location_id
			source_location_name
			destination_store
			delivery_date
			delivery_status_code
			delivery_type_code
			sales_order_reference
		download - when set to true returns an Excel download URL for the filtered results.
		Pagination is applied even when no parameters are provided.
	"""
	
	user = request.user
	queryset = AuthorizationService.filter_deliveries_for_user(
		user, InboundDelivery.objects.all()
	)
	download_requested = request.query_params.get('download', '').lower() == 'true'

	try:
		try:
			queryset = _apply_delivery_filters(queryset, request.query_params)
		except ValidationError as e:
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message=str(e)
			)

		queryset = queryset.order_by('-created_date')
		download_url = None
		if download_requested:
			download_url = _generate_search_export_link(queryset, request)
		
		paginator = CustomPagination()
		paginated_results = paginator.paginate_queryset(queryset, request)
		serializer = InboundDeliverySerializer(paginated_results, many=True)

		paginated_data = paginator.get_paginated_response(serializer.data).data
		if download_url:
			paginated_data['download_url'] = download_url
		
		return APIResponse(
			status=status.HTTP_200_OK,
			message='Inbound deliveries fetched successfully',
			data=paginated_data
		)
		
	except Exception as e:
		logger.error(f"Error searching deliveries: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message='Internal server error while searching deliveries'
		)


@api_view(['POST'])
@authentication_classes([CombinedAuthentication])
def create_delivery_receipt(request):
	"""
	Create a delivery receipt from an inbound delivery with comprehensive validation
	"""
	try:
		with transaction.atomic():
			# Get inbound delivery and authorize
			inbound_delivery_id = request.data.get('delivery')
			if not inbound_delivery_id:
				return APIResponse(
					status=status.HTTP_400_BAD_REQUEST,
					message='delivery is required'
				)
			try:
				inbound_delivery = InboundDelivery.objects.get(id=inbound_delivery_id)
			except InboundDelivery.DoesNotExist:
				return APIResponse(
					status=status.HTTP_404_NOT_FOUND,
					message='Inbound delivery not found'
				)
			# Authorization: user must have access to destination store
			if not AuthorizationService.validate_store_access(request.user, inbound_delivery.destination_store.byd_cost_center_code):
				return APIResponse(
					status=status.HTTP_403_FORBIDDEN,
					message=f"You are not authorized to receive inbound deliveries for store '{inbound_delivery.destination_store.store_name}'"
				)
			# Validate delivery status
			if inbound_delivery.delivery_status_code not in ['1', '2']:
				return APIResponse(
					status=status.HTTP_400_BAD_REQUEST,
					message=f"Cannot receive from inbound delivery in status: {inbound_delivery.delivery_status}"
				)
			# Validate input data using serializer with request context
			serializer = TransferReceiptNoteSerializer(data=request.data, context={'request': request})
			if not serializer.is_valid():
				return APIResponse(
					status=status.HTTP_400_BAD_REQUEST,
					message='Validation error',
					data=serializer.errors
				)
			# Create receipt and update inbound delivery via serializer
			receipt = serializer.save()
			response_serializer = TransferReceiptNoteSerializer(receipt)
			send_inbound_delivery_receipt_notification(inbound_delivery, receipt.line_items.all(), request.user, receipt.notes)
			
			return APIResponse(
				message=f"Transfer receipt TR-{receipt.receipt_number} created successfully",
				status=status.HTTP_201_CREATED,
				data=response_serializer.data
			)
	except ValidationError as e:
		logger.error(f"Validation error creating transfer receipt: {str(e)}")
		return APIResponse(
			status=status.HTTP_400_BAD_REQUEST,
			message='Validation error',
			data={'non_field_errors': [str(e)]}
		)
	except Exception as e:
		logger.error(f"Unexpected error creating transfer receipt: {str(e)}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message=f"An error occurred while creating the transfer receipt: {str(e)}"
		)


def send_inbound_delivery_receipt_notification(inbound_delivery, received_items, user, notes):
	"""
	Send email notification about delivery receipt creation
	"""
	try:
		# For warehouse-to-store transfers, notify destination store managers
		# and potentially warehouse managers (if StoreAuthorization exists for warehouse)
		destination_store = inbound_delivery.destination_store
		destination_managers = StoreAuthorization.objects.filter(
			store=destination_store,
			role__in=['manager', 'assistant']
		).select_related('user')
		
		manager_emails = [
			auth.user.email for auth in destination_managers 
			if auth.user.email
		]
		
		if not manager_emails:
			logger.warning(f"No email addresses found for managers of store {destination_store.store_name}")
			return False
		
		# Prepare email context
		context = {
			'inbound_delivery': inbound_delivery,
			'received_items': received_items,
			'received_by': user,
			'notes': notes,
			'destination_store': inbound_delivery.destination_store
		}
		
		# Render email content
		subject = f"Delivery Received - {inbound_delivery.delivery_id}"
		html_content = render_to_string('transfer_service/delivery_receipt_notification.html', context)

		# Send email
		email = EmailMessage(
			subject=subject,
			body=html_content,
			from_email='network@foodconceptsplc.com',
			to=["davynathaniel@gmail.com"],
		)
		email.content_subtype = 'html'
		email.send()
		
		logger.info(f"Delivery receipt notification sent for inbound delivery {inbound_delivery.delivery_id}")
		return True
		
	except Exception as e:
		logger.error(f"Error sending inbound delivery receipt notification: {str(e)}")
		return False


@api_view(['POST'])
@authentication_classes([CombinedAuthentication])
def approve_delivery_receipt(request, receipt_id):
	"""
	SCD_Team approves a receipt submitted by the receiving store.
	Only after approval can the receipt be synced to SAP ByD.
	"""
	try:
		# Only SCD_Team members can approve receipts
		if not AuthorizationService.is_scd_team_member(request.user):
			return APIResponse(
				status=status.HTTP_403_FORBIDDEN,
				message="Only SCD Team members can approve receipts"
			)

		try:
			receipt = TransferReceiptNote.objects.select_related(
				'inbound_delivery',
				'inbound_delivery__destination_store'
			).get(id=receipt_id)
		except TransferReceiptNote.DoesNotExist:
			return APIResponse(
				status=status.HTTP_404_NOT_FOUND,
				message=f"Receipt {receipt_id} not found"
			)

		# Validate the user is authorized for this receipt's source warehouse
		if not AuthorizationService.validate_source_location_access(
			request.user, receipt.inbound_delivery.source_location_id
		):
			return APIResponse(
				status=status.HTTP_403_FORBIDDEN,
				message="You are not authorized to approve receipts from this source location"
			)

		# Validate receipt is in a state that can be approved
		if receipt.approval_status not in ['receipt_submitted', 'resubmitted']:
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message=f"Receipt cannot be approved in status: {receipt.approval_status}"
			)

		# PPU/SCD supervisor must explicitly declare whether the delivery is complete
		# or still in progress, rather than leaving it in an inferred default status.
		is_completed = request.data.get('isCompleted')
		if not isinstance(is_completed, bool):
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message="'isCompleted' is required and must be true or false"
			)

		with transaction.atomic():
			# Update receipt to approved status
			receipt.approval_status = 'approved'
			receipt.approved_at = timezone.now()
			receipt.approved_by = request.user
			receipt.save()

			# Delivery status is the supervisor's explicit decision (isCompleted),
			# not inferred from received quantity.
			inbound_delivery = receipt.inbound_delivery
			inbound_delivery.refresh_from_db()
			inbound_delivery.delivery_status_code = '3' if is_completed else '2'  # 3=Completed, 2=In Process
			inbound_delivery.save()

			# Trigger SAP ByD sync asynchronously
			from django_q.tasks import async_task
			async_task('vimp.tasks.sync_approved_receipt_to_sap', receipt.id)

		# Send approval notification
		send_receipt_approval_notification(receipt, request.user)

		return APIResponse(
			status=status.HTTP_200_OK,
			message=f"Receipt TR-{receipt.receipt_number} approved successfully",
			data=TransferReceiptNoteSerializer(receipt).data
		)

	except Exception as e:
		logger.error(f"Error approving receipt {receipt_id}: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message=f"Error approving receipt: {str(e)}"
		)


@api_view(['POST'])
@authentication_classes([CombinedAuthentication])
def reject_delivery_receipt(request, receipt_id):
	"""
	SCD_Team rejects a receipt submitted by the receiving store.
	The receiving store must update and resubmit the receipt.
	"""
	try:
		# Only SCD_Team members can reject receipts
		if not AuthorizationService.is_scd_team_member(request.user):
			return APIResponse(
				status=status.HTTP_403_FORBIDDEN,
				message="Only SCD Team members can reject receipts"
			)

		try:
			receipt = TransferReceiptNote.objects.select_related(
				'inbound_delivery',
				'inbound_delivery__destination_store'
			).get(id=receipt_id)
		except TransferReceiptNote.DoesNotExist:
			return APIResponse(
				status=status.HTTP_404_NOT_FOUND,
				message=f"Receipt {receipt_id} not found"
			)

		# Validate the user is authorized for this receipt's source warehouse
		if not AuthorizationService.validate_source_location_access(
			request.user, receipt.inbound_delivery.source_location_id
		):
			return APIResponse(
				status=status.HTTP_403_FORBIDDEN,
				message="You are not authorized to reject receipts from this source location"
			)

		# Validate receipt is in a state that can be rejected
		if receipt.approval_status not in ['receipt_submitted', 'resubmitted']:
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message=f"Receipt cannot be rejected in status: {receipt.approval_status}"
			)

		# Validate rejection reason is provided
		rejection_reason = request.data.get('rejection_reason')
		if not rejection_reason:
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message="Rejection reason is required"
			)

		with transaction.atomic():
			# Update receipt to rejected status
			receipt.approval_status = 'rejected'
			receipt.rejection_reason = rejection_reason
			receipt.rejection_count += 1
			receipt.save()

		# Send rejection notification to receiving store
		send_receipt_rejection_notification(receipt, request.user, rejection_reason)

		return APIResponse(
			status=status.HTTP_200_OK,
			message=f"Receipt TR-{receipt.receipt_number} rejected",
			data={
				'receipt': TransferReceiptNoteSerializer(receipt).data,
				'rejection_reason': rejection_reason
			}
		)

	except Exception as e:
		logger.error(f"Error rejecting receipt {receipt_id}: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message=f"Error rejecting receipt: {str(e)}"
		)


@api_view(['PUT'])
@authentication_classes([CombinedAuthentication])
def update_rejected_receipt(request, receipt_id):
	"""
	Restaurant Manager updates a rejected receipt and resubmits for approval.
	"""
	from decimal import Decimal
	from .models import TransferReceiptLineItem

	try:
		# Only Restaurant_Manager members can update rejected receipts
		if not AuthorizationService.is_restaurant_manager(request.user):
			return APIResponse(
				status=status.HTTP_403_FORBIDDEN,
				message="Only Restaurant Managers can update rejected receipts"
			)

		try:
			receipt = TransferReceiptNote.objects.select_related(
				'inbound_delivery',
				'inbound_delivery__destination_store'
			).get(id=receipt_id)
		except TransferReceiptNote.DoesNotExist:
			return APIResponse(
				status=status.HTTP_404_NOT_FOUND,
				message=f"Receipt {receipt_id} not found"
			)

		# Validate receipt is in rejected status
		if receipt.approval_status != 'rejected':
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message=f"Only rejected receipts can be updated. Current status: {receipt.approval_status}"
			)

		# Validate line items are provided
		line_items_data = request.data.get('line_items', [])
		if not line_items_data:
			return APIResponse(
				status=status.HTTP_400_BAD_REQUEST,
				message="Line items are required for update"
			)

		with transaction.atomic():
			# Update line items
			for item_data in line_items_data:
				try:
					# Look up by delivery line item ID (what frontend sends)
					line_item = TransferReceiptLineItem.objects.get(
						inbound_delivery_line_item_id=item_data.get('line_item_id'),
						transfer_receipt=receipt
					)
				except TransferReceiptLineItem.DoesNotExist:
					return APIResponse(
						status=status.HTTP_400_BAD_REQUEST,
						message=f"Line item {item_data.get('line_item_id')} not found in this receipt"
					)

				# Calculate old and new quantities
				old_quantity = line_item.quantity_received
				new_quantity = Decimal(str(item_data.get('quantity_received', old_quantity)))

				# Update delivery line item received quantity
				delivery_line = line_item.inbound_delivery_line_item
				delivery_line.quantity_received = (
					delivery_line.quantity_received - old_quantity + new_quantity
				)

				# Validate total doesn't exceed expected
				if delivery_line.quantity_received > delivery_line.quantity_expected:
					return APIResponse(
						status=status.HTTP_400_BAD_REQUEST,
						message=f"Total received ({delivery_line.quantity_received}) exceeds expected ({delivery_line.quantity_expected}) for {delivery_line.product_name}"
					)

				delivery_line.save()

				# Update the receipt line item
				line_item.quantity_received = new_quantity
				line_item.save()

			# Update receipt metadata with update history
			update_history = receipt.metadata.get('update_history', [])
			update_history.append({
				'updated_at': timezone.now().isoformat(),
				'updated_by': request.user.username,
				'previous_status': 'rejected',
				'previous_rejection_reason': receipt.rejection_reason
			})
			receipt.metadata['update_history'] = update_history

			# Update receipt status to resubmitted
			receipt.approval_status = 'resubmitted'
			receipt.submitted_at = timezone.now()
			receipt.notes = request.data.get('notes', receipt.notes)
			receipt.save()

		# Notify sending store of resubmission
		send_receipt_resubmission_notification(receipt, request.user)

		return APIResponse(
			status=status.HTTP_200_OK,
			message=f"Receipt TR-{receipt.receipt_number} updated and resubmitted for approval",
			data=TransferReceiptNoteSerializer(receipt).data
		)

	except Exception as e:
		logger.error(f"Error updating receipt {receipt_id}: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message=f"Error updating receipt: {str(e)}"
		)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_pending_approvals(request):
	"""
	Get receipts pending approval for the source locations (warehouses) the
	user is assigned to via SourceLocationAuthorization.
	"""
	try:
		user = request.user
		authorized_locations = AuthorizationService.get_user_authorized_source_locations(user)

		if not authorized_locations:
			return APIResponse(
				status=status.HTTP_200_OK,
				message='No pending approvals',
				data={'count': 0, 'results': []}
			)

		pending_receipts = TransferReceiptNote.objects.filter(
			approval_status__in=['receipt_submitted', 'resubmitted'],
			inbound_delivery__source_location_id__in=authorized_locations,
		).select_related(
			'inbound_delivery',
			'inbound_delivery__destination_store',
			'created_by'
		).order_by('-submitted_at')

		paginator = CustomPagination()
		paginated_receipts = paginator.paginate_queryset(pending_receipts, request)
		serializer = TransferReceiptNoteSerializer(paginated_receipts, many=True)

		return APIResponse(
			status=status.HTTP_200_OK,
			message='Pending approvals fetched successfully',
			data=paginator.get_paginated_response(serializer.data).data
		)

	except Exception as e:
		logger.error(f"Error fetching pending approvals: {e}")
		return APIResponse(
			status=status.HTTP_500_INTERNAL_SERVER_ERROR,
			message='Error fetching pending approvals'
		)


def send_receipt_approval_notification(receipt, approved_by):
	"""
	Send notification to receiving store about receipt approval.
	"""
	try:
		inbound_delivery = receipt.inbound_delivery
		destination_store = inbound_delivery.destination_store

		# Get receiving store managers
		destination_managers = StoreAuthorization.objects.filter(
			store=destination_store,
			role__in=['manager', 'assistant']
		).select_related('user')

		manager_emails = [
			auth.user.email for auth in destination_managers
			if auth.user.email
		]

		if not manager_emails:
			logger.warning(f"No email addresses found for managers of store {destination_store.store_name}")
			return False

		context = {
			'receipt': receipt,
			'inbound_delivery': inbound_delivery,
			'approved_by': approved_by,
			'destination_store': destination_store
		}

		subject = f"Receipt Approved - TR-{receipt.receipt_number}"
		html_content = render_to_string('transfer_service/receipt_approval_notification.html', context)

		email = EmailMessage(
			subject=subject,
			body=html_content,
			from_email='network@foodconceptsplc.com',
			to=manager_emails,
		)
		email.content_subtype = 'html'
		email.send()

		logger.info(f"Approval notification sent for receipt TR-{receipt.receipt_number}")
		return True

	except Exception as e:
		logger.error(f"Error sending approval notification: {str(e)}")
		return False


def send_receipt_rejection_notification(receipt, rejected_by, rejection_reason):
	"""
	Send notification to receiving store about receipt rejection.
	"""
	try:
		inbound_delivery = receipt.inbound_delivery
		destination_store = inbound_delivery.destination_store

		# Get receiving store managers
		destination_managers = StoreAuthorization.objects.filter(
			store=destination_store,
			role__in=['manager', 'assistant']
		).select_related('user')

		manager_emails = [
			auth.user.email for auth in destination_managers
			if auth.user.email
		]

		if not manager_emails:
			logger.warning(f"No email addresses found for managers of store {destination_store.store_name}")
			return False

		context = {
			'receipt': receipt,
			'inbound_delivery': inbound_delivery,
			'rejected_by': rejected_by,
			'rejection_reason': rejection_reason,
			'destination_store': destination_store
		}

		subject = f"Receipt Rejected - TR-{receipt.receipt_number} - Action Required"
		html_content = render_to_string('transfer_service/receipt_rejection_notification.html', context)

		email = EmailMessage(
			subject=subject,
			body=html_content,
			from_email='network@foodconceptsplc.com',
			to=manager_emails,
		)
		email.content_subtype = 'html'
		email.send()

		logger.info(f"Rejection notification sent for receipt TR-{receipt.receipt_number}")
		return True

	except Exception as e:
		logger.error(f"Error sending rejection notification: {str(e)}")
		return False


def send_receipt_resubmission_notification(receipt, resubmitted_by):
	"""
	Send notification to sending store about receipt resubmission.
	"""
	try:
		inbound_delivery = receipt.inbound_delivery
		source_location_id = inbound_delivery.source_location_id

		# Get sending store managers (using StoreAuthorization for warehouses treated as stores)
		from egrn_service.models import Store
		try:
			source_store = Store.objects.get(byd_cost_center_code=source_location_id)
			source_managers = StoreAuthorization.objects.filter(
				store=source_store,
				role__in=['manager', 'assistant']
			).select_related('user')

			manager_emails = [
				auth.user.email for auth in source_managers
				if auth.user.email
			]
		except Store.DoesNotExist:
			logger.warning(f"Source location {source_location_id} not found as a store")
			manager_emails = []

		if not manager_emails:
			logger.warning(f"No email addresses found for managers of source location {source_location_id}")
			return False

		context = {
			'receipt': receipt,
			'inbound_delivery': inbound_delivery,
			'resubmitted_by': resubmitted_by,
			'destination_store': inbound_delivery.destination_store
		}

		subject = f"Receipt Resubmitted - TR-{receipt.receipt_number} - Review Required"
		html_content = render_to_string('transfer_service/receipt_resubmission_notification.html', context)

		email = EmailMessage(
			subject=subject,
			body=html_content,
			from_email='network@foodconceptsplc.com',
			to=manager_emails,
		)
		email.content_subtype = 'html'
		email.send()

		logger.info(f"Resubmission notification sent for receipt TR-{receipt.receipt_number}")
		return True

	except Exception as e:
		logger.error(f"Error sending resubmission notification: {str(e)}")
		return False