import time as time_module

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from shop.models import Order, PathaoShipment, PathaoStatusSyncRun, SiteSettings
from shop.pathao import PathaoApiError, get_order_info, is_pathao_configured

REQUEST_PAUSE_SECONDS = 5


class Command(BaseCommand):
    help = (
        'Refresh pending Pathao shipment statuses one at a time for a queued manual sync.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--run-id',
            type=int,
            required=True,
            help='Run an already queued immediate sync.',
        )

    def handle(self, *args, **options):
        site_settings = SiteSettings.objects.first()
        if site_settings is None:
            if options['run_id'] is not None:
                PathaoStatusSyncRun.objects.filter(
                    pk=options['run_id'],
                    status='queued',
                ).update(
                    status='failed',
                    error_summary='Configure Site Settings before syncing Pathao shipments.',
                    completed_at=timezone.now(),
                )
            raise CommandError('Configure Site Settings before syncing Pathao shipments.')
        if not is_pathao_configured(site_settings):
            if options['run_id'] is not None:
                PathaoStatusSyncRun.objects.filter(
                    pk=options['run_id'],
                    status='queued',
                ).update(
                    status='failed',
                    error_summary='Configure Pathao credentials in Site Settings first.',
                    completed_at=timezone.now(),
                )
            raise CommandError('Configure Pathao credentials in Site Settings first.')

        run_id = options['run_id']
        run = self._get_run(run_id, site_settings)
        if run is None:
            return
        try:
            self._run_sync(run, site_settings)
        except Exception as error:
            run.status = 'failed'
            run.error_summary = f'Pathao status sync stopped unexpectedly: {error}'[:10000]
            run.completed_at = timezone.now()
            run.save(update_fields=['status', 'error_summary', 'completed_at'])
            raise

    def _get_run(self, run_id, site_settings):
        try:
            return PathaoStatusSyncRun.objects.get(pk=run_id, status='queued')
        except PathaoStatusSyncRun.DoesNotExist as error:
            raise CommandError(f'Queued Pathao status sync run {run_id} was not found.') from error

    def _run_sync(self, run, site_settings):
        with transaction.atomic():
            claimed = PathaoStatusSyncRun.objects.filter(
                pk=run.pk,
                status='queued',
            ).update(
                status='running',
                started_at=timezone.now(),
            )
        if not claimed:
            raise CommandError(f'Pathao status sync run {run.pk} is no longer queued.')
        run.status = 'running'
        run.started_at = timezone.now()
        shipments = PathaoShipment.objects.filter(
            environment__in=(site_settings.pathao_environment, ''),
            consignment_id__gt='',
            order_status__iexact='Pending',
        )

        run.total_shipments = shipments.count()
        run.save(update_fields=['total_shipments'])
        errors = []
        for index, shipment in enumerate(shipments.iterator()):
            checked_at = timezone.now()
            PathaoShipment.objects.filter(pk=shipment.pk).update(
                status_checked_at=checked_at,
                updated_at=checked_at,
            )
            try:
                shipment_info = get_order_info(
                    site_settings,
                    shipment.consignment_id,
                )
            except PathaoApiError as error:
                run.failed_shipments += 1
                error_message = str(error)[:2000]
                errors.append(f'{shipment.consignment_id}: {error_message}')
                PathaoShipment.objects.filter(pk=shipment.pk).update(
                    info_error=error_message,
                    updated_at=timezone.now(),
                )
                self.stderr.write(self.style.ERROR(
                    f'Could not refresh Pathao shipment {shipment.consignment_id}: {error}'
                ))
            else:
                update_fields = ['info_error', 'updated_at']
                shipment.info_error = ''
                if 'order_status' in shipment_info:
                    shipment.order_status = shipment_info['order_status']
                    update_fields.append('order_status')
                    Order.objects.filter(
                        pk=shipment.order_id,
                    ).update(pathao_order_status=shipment_info['order_status'])
                if 'delivery_fee' in shipment_info:
                    shipment.delivery_fee = shipment_info['delivery_fee']
                    update_fields.append('delivery_fee')
                shipment.save(update_fields=update_fields)
                run.checked_shipments += 1

            run.error_summary = '\n'.join(errors)[:10000]
            run.save(update_fields=[
                'checked_shipments',
                'failed_shipments',
                'error_summary',
            ])
            if index < run.total_shipments - 1:
                time_module.sleep(REQUEST_PAUSE_SECONDS)

        run.status = 'completed'
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'completed_at'])
        self.stdout.write(self.style.SUCCESS(
            f'Pathao status sync complete: {run.checked_shipments} updated, '
            f'{run.failed_shipments} failed.'
        ))
