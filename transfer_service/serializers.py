from rest_framework import serializers
from .models import (
    TransferReceiptNote, TransferReceiptLineItem,
    InboundDelivery, InboundDeliveryLineItem
)


class InboundDeliveryLineItemSerializer(serializers.ModelSerializer):
    # Computed fields
    quantity_outstanding = serializers.ReadOnlyField()
    is_fully_received = serializers.ReadOnlyField()
    total_value = serializers.SerializerMethodField()

    class Meta:
        model = InboundDeliveryLineItem
        fields = [
            'id', 'object_id', 'product_id', 'product_name',
            'quantity_expected', 'quantity_received', 'unit_of_measurement',
            'unit_price', 'total_value',
            'quantity_outstanding', 'is_fully_received', 'metadata'
        ]

    def get_total_value(self, obj):
        """Calculate total expected value (quantity_expected * unit_price)"""
        return float(obj.quantity_expected) * float(obj.unit_price or 0)


class ReceiptLineItemSummarySerializer(serializers.Serializer):
    """Lightweight serializer for receipt line items when nested in receipt summary"""
    delivery_line_item = serializers.IntegerField(source='inbound_delivery_line_item_id')
    quantity_received = serializers.DecimalField(max_digits=15, decimal_places=3)


class InboundDeliveryReceiptSummarySerializer(serializers.ModelSerializer):
    """Lightweight serializer for receipts when nested in delivery responses"""
    created_by = serializers.CharField(source='created_by.get_full_name', read_only=True)
    approval_status_display = serializers.SerializerMethodField()
    approved_by_name = serializers.CharField(source='approved_by.get_full_name', read_only=True, allow_null=True)
    line_items = ReceiptLineItemSummarySerializer(many=True, read_only=True)

    class Meta:
        model = None  # Will be set after TransferReceiptNote import
        fields = [
            'id', 'receipt_number', 'line_items', 'notes', 'created_date', 'created_by',
            'approval_status', 'approval_status_display', 'submitted_at',
            'approved_at', 'approved_by_name', 'rejection_reason', 'rejection_count',
            'synced_to_sap'
        ]

    def get_approval_status_display(self, obj):
        """Get human-readable approval status"""
        from .models import RECEIPT_APPROVAL_STATUS_CHOICES
        return dict(RECEIPT_APPROVAL_STATUS_CHOICES).get(obj.approval_status, obj.approval_status)


# Set the model after imports to avoid circular dependency
from .models import TransferReceiptNote
InboundDeliveryReceiptSummarySerializer.Meta.model = TransferReceiptNote


class InboundDeliverySerializer(serializers.ModelSerializer):
    line_items = InboundDeliveryLineItemSerializer(many=True, read_only=True)
    receipts = InboundDeliveryReceiptSummarySerializer(many=True, read_only=True)

    # Computed fields
    delivery_status = serializers.ReadOnlyField()
    total_quantity_expected = serializers.ReadOnlyField()
    total_quantity_received = serializers.ReadOnlyField()
    is_fully_received = serializers.ReadOnlyField()

    # Store and location details
    destination_store_name = serializers.CharField(source='destination_store.store_name', read_only=True)

    # Approval summary fields
    latest_receipt_status = serializers.SerializerMethodField()
    has_pending_approval = serializers.SerializerMethodField()

    def get_destination_store(self, obj):
        return Store.objects.get(id=obj.destination_store.id)

    def get_latest_receipt_status(self, obj):
        """Get the approval status of the most recent receipt"""
        latest_receipt = obj.receipts.order_by('-created_date').first()
        if latest_receipt:
            from .models import RECEIPT_APPROVAL_STATUS_CHOICES
            return {
                'status': latest_receipt.approval_status,
                'status_display': dict(RECEIPT_APPROVAL_STATUS_CHOICES).get(
                    latest_receipt.approval_status, latest_receipt.approval_status
                ),
                'receipt_number': latest_receipt.receipt_number
            }
        return None

    def get_has_pending_approval(self, obj):
        """Check if delivery has any receipts pending approval"""
        return obj.receipts.filter(approval_status__in=['receipt_submitted', 'resubmitted']).exists()

    class Meta:
        model = InboundDelivery
        fields = [
            'id', 'object_id', 'delivery_id', 'source_location_id', 'source_location_name',
            'destination_store', 'delivery_date', 'delivery_status_code', 'delivery_status',
            'delivery_type_code', 'sales_order_reference', 'total_quantity_expected',
            'total_quantity_received', 'is_fully_received', 'destination_store_name',
            'line_items', 'receipts', 'latest_receipt_status', 'has_pending_approval',
            'metadata', 'created_date'
        ]


class TransferReceiptLineItemSerializer(serializers.ModelSerializer):
    # Product details from related inbound delivery line item
    inbound_delivery_line_item = InboundDeliveryLineItemSerializer(read_only=True)
    product_id = serializers.ReadOnlyField(source='inbound_delivery_line_item.product_id')
    value_received = serializers.ReadOnlyField()

    # Inbound delivery line item details
    quantity_expected = serializers.DecimalField(
        source='inbound_delivery_line_item.quantity_expected',
        max_digits=15, decimal_places=3, read_only=True
    )
    unit_of_measurement = serializers.CharField(
        source='inbound_delivery_line_item.unit_of_measurement',
        read_only=True
    )
    unit_price = serializers.DecimalField(
        source='inbound_delivery_line_item.unit_price',
        max_digits=15, decimal_places=3, read_only=True
    )

    class Meta:
        model = TransferReceiptLineItem
        fields = [
            'id', 'inbound_delivery_line_item', 'product_id',
            'quantity_expected', 'quantity_received', 'unit_price',
            'unit_of_measurement', 'value_received', 'metadata'
        ]
    
    def validate_quantity_received(self, value):
        """Validate that received quantity is valid"""
        if value <= 0:
            raise serializers.ValidationError("Quantity received must be greater than 0")
        return value
    
    def validate(self, attrs):
        """Validate the entire line item"""
        inbound_delivery_line_item = attrs.get('inbound_delivery_line_item')
        quantity_received = attrs.get('quantity_received')
        
        if inbound_delivery_line_item and quantity_received:
            # Check if receiving this quantity would exceed the expected quantity
            current_received = inbound_delivery_line_item.quantity_received
            total_received = current_received + quantity_received
            
            if total_received > inbound_delivery_line_item.quantity_expected:
                raise serializers.ValidationError({
                    'quantity_received': f'Total received quantity ({total_received}) would exceed expected quantity ({inbound_delivery_line_item.quantity_expected})'
                })
        
        return attrs


class TransferReceiptNoteSerializer(serializers.ModelSerializer):
    line_items = TransferReceiptLineItemSerializer(many=True, read_only=True)

    # Store and user details
    destination_store = serializers.CharField(source='inbound_delivery.destination_store.store_name', read_only=True)
    created_by = serializers.CharField(source='created_by.get_full_name', read_only=True)

    # Inbound delivery details
    inbound_delivery = InboundDeliverySerializer(read_only=True)
    source_location = serializers.CharField(source='inbound_delivery.source_location_name', read_only=True)
    source_location_id = serializers.CharField(source='inbound_delivery.source_location_id', read_only=True)

    # Approval workflow fields
    approval_status_display = serializers.SerializerMethodField()
    approved_by_name = serializers.CharField(source='approved_by.get_full_name', read_only=True, allow_null=True)
    total_value_received = serializers.ReadOnlyField()

    class Meta:
        model = TransferReceiptNote
        fields = [
            'id', 'receipt_number', 'inbound_delivery', 'notes', 'source_location', 'source_location_id', 'destination_store',
            'created_date', 'created_by', 'line_items', 'metadata',
            # Approval workflow fields
            'approval_status', 'approval_status_display', 'submitted_at', 'approved_at',
            'approved_by_name', 'rejection_reason', 'rejection_count', 'synced_to_sap',
            'total_value_received'
        ]
        read_only_fields = ['inbound_delivery', 'receipt_number', 'created_by', 'approval_status',
                          'submitted_at', 'approved_at', 'approved_by_name', 'rejection_reason',
                          'rejection_count', 'synced_to_sap']

    def get_approval_status_display(self, obj):
        """Get human-readable approval status"""
        from .models import RECEIPT_APPROVAL_STATUS_CHOICES
        return dict(RECEIPT_APPROVAL_STATUS_CHOICES).get(obj.approval_status, obj.approval_status)

    def validate(self, data):
        """Validate the entire inbound delivery receipt"""
        inbound_delivery_id = self.initial_data.get('delivery')
        if not inbound_delivery_id:
            raise serializers.ValidationError({"delivery": ["delivery is required"]})
        try:
            inbound_delivery = InboundDelivery.objects.get(id=inbound_delivery_id)
        except InboundDelivery.DoesNotExist:
            raise serializers.ValidationError({"delivery": ["Inbound delivery not found"]})

        # eGRN 2: a sales order is received once. SCD approval posts the outbound
        # delivery for what was received; a second receipt could not be posted.
        if inbound_delivery.sales_order_reference:
            open_receipt = inbound_delivery.receipts.exclude(approval_status='rejected').first()
            if open_receipt:
                raise serializers.ValidationError({
                    "delivery": [
                        f"Sales order {inbound_delivery.sales_order_reference} already has receipt "
                        f"TR-{open_receipt.receipt_number} ({open_receipt.approval_status}); "
                        f"only one receipt is allowed per sales order"
                    ]
                })

        inbound_delivery_line_items = inbound_delivery.line_items.all()
        received_line_items = self.initial_data.get('line_items', [])
        
        if not isinstance(received_line_items, list) or not received_line_items:
            raise serializers.ValidationError({"line_items": ["At least one line item is required"]})

        # Ensure all line items belong to this delivery
        for item in received_line_items:
            delivery_line_item_id = item.get('delivery_line_item')
            if delivery_line_item_id is None:
                raise serializers.ValidationError({"line_items": ["Each item must include delivery_line_item"]})
            inbound_delivery_line_item = inbound_delivery_line_items.filter(
                id=delivery_line_item_id
            ).first()
            if not inbound_delivery_line_item:
                raise serializers.ValidationError(
                    {"line_items": ["All line items must belong to the specified inbound delivery"]}
                )
            qty = item.get('quantity_received')
            if qty is None:
                raise serializers.ValidationError({"line_items": ["Each item must include quantity_received"]})
            try:
                from decimal import Decimal
                qty_dec = Decimal(str(qty))
            except Exception:
                raise serializers.ValidationError({"line_items": ["quantity_received must be a number"]})
            if qty_dec <= 0:
                raise serializers.ValidationError({"line_items": ["quantity_received must be greater than 0"]})
            total_received = inbound_delivery_line_item.quantity_received + qty_dec
            if total_received > inbound_delivery_line_item.quantity_expected:
                raise serializers.ValidationError({
                    "line_items": [
                        f"Total received ({total_received}) exceeds expected ({inbound_delivery_line_item.quantity_expected}) for {inbound_delivery_line_item.product_name}"
                    ]
                })

        return data
    
    def create(self, validated_data):
        """Create inbound delivery receipt note with line items"""
        from django.db import transaction
        from django.utils import timezone
        from decimal import Decimal
        request = self.context.get('request')
        user = getattr(request, 'user', None)

        inbound_delivery_id = self.initial_data.get('delivery')
        notes = self.initial_data.get('notes')
        line_items = self.initial_data.get('line_items', [])

        inbound_delivery = InboundDelivery.objects.get(id=inbound_delivery_id)

        with transaction.atomic():
            # Create the transfer receipt note with approval status
            receipt = TransferReceiptNote(
                inbound_delivery=inbound_delivery,
                notes=notes,
                created_by=user,
                approval_status='receipt_submitted',
                submitted_at=timezone.now()
            )
            receipt.save()

            # Create line items and update inbound delivery line items
            for item in line_items:
                inbound_delivery_line_item = InboundDeliveryLineItem.objects.get(
                    id=item.get('delivery_line_item')
                )
                quantity_received = Decimal(str(item.get('quantity_received')))

                # Create TR line item
                TransferReceiptLineItem.objects.create(
                    transfer_receipt=receipt,
                    inbound_delivery_line_item=inbound_delivery_line_item,
                    quantity_received=quantity_received,
                    metadata=item.get('metadata', {})
                )

                # Update inbound delivery line item received quantity
                inbound_delivery_line_item.quantity_received = (
                    Decimal(str(inbound_delivery_line_item.quantity_received)) + quantity_received
                )
                inbound_delivery_line_item.save()

            # Note: Do NOT update delivery status to Completed here
            # The status will only be updated after sending store approval
            # Just set to 'In Process' if not already
            inbound_delivery.refresh_from_db()
            if inbound_delivery.delivery_status_code == '1':  # Open
                inbound_delivery.delivery_status_code = '2'  # In Process
                inbound_delivery.save()

            return receipt