# Tests del job recordatorios Cleanest: solo funciones puras (sin red ni DB).
from datetime import date

from jobs import cleanest_recordatorios as job


def _o(n, sku, fecha, status="Pendiente", cantidad=2):
    return {"numero_orden": n, "sku": sku, "cantidad": cantidad,
            "fecha_promesa": fecha, "status": status}


def test_parte_vencidas_y_proximas():
    hoy = date(2026, 10, 8)
    v, p = job.partir_por_vencimiento(
        [_o("A", "S1", "2026-10-05"), _o("B", "S1", "2026-10-09"),
         _o("C", "S1", "2026-12-01"), _o("D", "", "2026-10-09")], hoy)
    assert [o["numero_orden"] for o in v] == ["A"]
    assert [o["numero_orden"] for o in p] == ["B"]


def test_decidir_solo_repite_si_vence_pronto():
    hoy = date(2026, 10, 8)
    prox = [_o("B", "S1", "2026-10-09"), _o("C", "S1", "2026-10-15")]
    inc, omit = job.decidir_incluir(prox, hoy, {"B", "C"})
    assert [o["numero_orden"] for o in inc] == ["B"] and omit == 1
    inc2, omit2 = job.decidir_incluir(prox, hoy, set())
    assert len(inc2) == 2 and omit2 == 0


def test_mensaje_compacto_con_vencidas():
    hoy = date(2026, 10, 8)
    chunks = job.build_mensaje([_o("A", "S1", "2026-10-05")], [_o("B", "S2", "2026-10-09")])
    texto = "\n".join(chunks)
    assert "Vencidas (1)" in texto and "Próximas (1)" in texto
    assert "<code>A</code>" in texto and "×2" in texto


def test_mensaje_sin_pendientes():
    assert "Sin órdenes" in "\n".join(job.build_mensaje([], []))
