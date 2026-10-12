# El scheduler debe disparar en hora CDMX, no UTC (Easypanel corre en UTC).
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

CDMX = ZoneInfo("America/Mexico_City")


def test_trigger_meli_dispara_12_05_cdmx():
    antes = datetime(2026, 10, 8, 10, 0, tzinfo=CDMX)
    nxt = CronTrigger(hour=12, minute=5, timezone=CDMX).get_next_fire_time(None, antes)
    assert (nxt.hour, nxt.minute) == (12, 5)
    assert str(nxt.tzinfo) == str(CDMX)


def test_trigger_explicito_respeta_cdmx_aunque_ref_sea_utc():
    """Caso prod: instante en UTC, el disparo debe ser 12:05 CDMX (=18:05 UTC)."""
    antes_utc = datetime(2026, 10, 8, 16, 0, tzinfo=ZoneInfo("UTC"))
    nxt = CronTrigger(hour=12, minute=5, timezone=CDMX).get_next_fire_time(None, antes_utc)
    assert nxt.astimezone(CDMX).hour == 12 and nxt.astimezone(CDMX).minute == 5
    assert nxt.astimezone(ZoneInfo("UTC")).hour == 18


def test_main_pasa_timezone_a_todos_los_triggers():
    import io
    src = io.open("main.py", encoding="utf-8").read()
    sin_tz = [ln for ln in src.splitlines()
              if "CronTrigger(hour=int(" in ln and "timezone=" not in ln]
    assert sin_tz == [], sin_tz
    assert src.count("timezone=TZ_CDMX") >= 7
