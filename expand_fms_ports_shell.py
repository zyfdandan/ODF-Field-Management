#!/usr/bin/env python3
"""Expand FMS front ports to match fiber strand counts (run on NetBox server).

Usage:
  docker cp expand_fms_ports_shell.py netbox:/tmp/
  docker exec netbox python /opt/netbox/netbox/manage.py shell -c "exec(open('/tmp/expand_fms_ports_shell.py').read())"
"""

from dcim.models import CableTermination, Device, FrontPort, PortMapping, RearPort
from django.contrib.contenttypes.models import ContentType

from netbox_fms.models import FiberCable
from netbox_fms.services import _determine_cable_end
from netbox_fms.signals import fms_portmapping_bypass


def devices_for_cable(cable_id: int) -> list[Device]:
    rp_ct = ContentType.objects.get_for_model(RearPort)
    dev_ids: set[int] = set()
    for term in CableTermination.objects.filter(cable_id=cable_id, termination_type=rp_ct):
        rp = RearPort.objects.filter(pk=term.termination_id).first()
        if rp:
            dev_ids.add(rp.device_id)
    return list(Device.objects.filter(pk__in=dev_ids).order_by("id"))


def expand_fc(fc: FiberCable, device: Device, port_type: str = "splice") -> tuple[int, int]:
    cable_label = str(fc.cable) if fc.cable_id else f"FiberCable-{fc.pk}"
    strands = list(fc.fiber_strands.order_by("position"))
    strand_count = len(strands)
    if strand_count == 0:
        return 0, 0

    cable_end = _determine_cable_end(fc.cable, device) if fc.cable_id else "A"
    strand_fk = "front_port_b" if cable_end == "B" else "front_port_a"

    with fms_portmapping_bypass():
        rear_port, _ = RearPort.objects.get_or_create(
            device=device,
            name=cable_label,
            defaults={"type": port_type, "positions": strand_count},
        )
        if rear_port.positions != strand_count:
            rear_port.positions = strand_count
            rear_port.save(update_fields=["positions"])

        existing = dict(
            PortMapping.objects.filter(rear_port=rear_port).values_list("rear_port_position", "front_port_id")
        )
        created = 0
        for strand in strands:
            if strand.position in existing:
                fp_id = existing[strand.position]
                if not getattr(strand, f"{strand_fk}_id"):
                    setattr(strand, strand_fk, FrontPort.objects.get(pk=fp_id))
                    strand.save(update_fields=[strand_fk])
                continue
            fp = FrontPort(device=device, name=strand.name, type=port_type, color=strand.color)
            fp.save()
            PortMapping.objects.create(
                device=device,
                front_port=fp,
                rear_port=rear_port,
                front_port_position=1,
                rear_port_position=strand.position,
            )
            setattr(strand, strand_fk, fp)
            strand.save(update_fields=[strand_fk])
            created += 1
    return len(existing) + created, created


total_created = 0
for fc in FiberCable.objects.select_related("cable").order_by("id"):
    if not fc.cable_id:
        continue
    label = str(fc.cable)
    for device in devices_for_cable(fc.cable_id):
        got, created = expand_fc(fc, device)
        if created:
            print(f"  {label} @ {device.name}: +{created} ports (total {got})")
            total_created += created

print(f"Done. Created {total_created} front ports.")
