"""
Generador de PDF de cotizaciones para Zeutica.

Reproduce el formato del documento ejemplo:
- Encabezado con logo/datos de la empresa y título COTIZACION ZTC-xxx
- Tabla de datos del cliente
- Tabla de items (SKU, descripción, cantidad, precio, total)
- Bloque de totales (subtotal, envío, IVA, total)
- Términos y condiciones + pie de página

Depende solo de fpdf 1.7.2 (pyfpdf), que ya está en requirements.
La función principal `generar_pdf_cotizacion` devuelve los bytes del PDF.
"""
from datetime import datetime
from fpdf import FPDF

# Paleta y datos fijos de la empresa
AZUL = (31, 78, 121)        # azul corporativo (banners / encabezados de tabla)
GRIS = (230, 230, 230)      # gris claro para la fila TOTAL
NEGRO = (0, 0, 0)

EMPRESA_DOMICILIO = "Domicilio: Blvd De los Charros 1629 Belenes Norte Cp 45145, Zapopan, Jalisco."
EMPRESA_WEB = "www.zeutica.com"
EMPRESA_TEL = "Telefono: 33-1299-5688"
EMPRESA_EMAIL = "E-mail: ventas1@zeutica.com"
VALIDO_HASTA = "7 Dias"


def _dinero(valor) -> str:
    """Formatea un número como moneda: 8700 -> $8,700.00"""
    return f"${float(valor):,.2f}"


class _CotizacionPDF(FPDF):
    """PDF con pie de página fijo replicando el ejemplo."""

    def footer(self):
        self.set_y(-25)
        self.set_font("Arial", "B", 8)
        self.set_text_color(*NEGRO)
        self.cell(0, 5, "Si tienes alguna pregunta por favor contactanos", 0, 1, "C")
        self.set_font("Arial", "", 8)
        self.cell(0, 5, "Tel: 33-1299-5688 / E-mail: ventas1@zeutica.com", 0, 1, "C")
        self.set_y(-12)
        self.set_font("Arial", "", 8)
        self.cell(0, 5, f"Pagina {self.page_no()}", 0, 0, "R")


def generar_pdf_cotizacion(cot) -> bytes:
    """
    Construye el PDF de la cotización y devuelve su contenido binario (bytes).

    `cot` es el objeto CotizacionSchema (o cualquiera con los mismos atributos:
    codigo_cotizacion, empresa, atencion, email, domicilio, telefono, subtotal,
    iva, total, costo_envio, forma_pago, comentarios, usuario, items).
    """
    pdf = _CotizacionPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=30)
    pdf.add_page()
    ancho = pdf.w - pdf.l_margin - pdf.r_margin  # ancho útil (~190mm)

    # ---------- ENCABEZADO ----------
    # Logo (texto) + título a la derecha en la misma línea
    y_top = pdf.get_y()
    pdf.set_font("Arial", "B", 28)
    pdf.set_text_color(*AZUL)
    pdf.cell(90, 12, "Zeutica", 0, 0, "L")

    pdf.set_font("Arial", "B", 20)
    pdf.set_text_color(*NEGRO)
    pdf.cell(0, 12, f"COTIZACION {cot.codigo_cotizacion}", 0, 1, "R")

    # Datos de contacto de la empresa (izquierda)
    pdf.set_font("Arial", "", 8)
    pdf.set_text_color(*NEGRO)
    y_datos = pdf.get_y()
    for linea in (EMPRESA_DOMICILIO, EMPRESA_WEB, EMPRESA_TEL, EMPRESA_EMAIL,
                  f"Asesor: {cot.usuario}"):
        pdf.cell(115, 5, linea, 0, 1, "L")

    # Tabla Fecha / Válido Hasta (derecha), alineada con los datos de contacto
    x_tabla = pdf.l_margin + 120
    fecha_hoy = datetime.now().strftime("%d/%m/%Y")
    pdf.set_xy(x_tabla, y_datos)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(30, 7, "Fecha:", 1, 0, "L")
    pdf.set_font("Arial", "", 9)
    pdf.cell(40, 7, fecha_hoy, 1, 1, "R")
    pdf.set_x(x_tabla)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(30, 7, "Valido Hasta:", 1, 0, "L")
    pdf.set_font("Arial", "", 9)
    pdf.cell(40, 7, VALIDO_HASTA, 1, 1, "R")

    pdf.ln(6)

    # ---------- BANNER CLIENTE ----------
    pdf.set_fill_color(*AZUL)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(ancho, 8, "  CLIENTE", 0, 1, "L", fill=True)

    # Datos del cliente
    pdf.set_text_color(*NEGRO)
    campos = [
        ("NOMBRE:", cot.atencion),
        ("EMPRESA:", cot.empresa),
        ("EMAIL:", cot.email),
        ("DOMICILIO:", cot.domicilio or ""),
        ("TELEFONO:", cot.telefono),
    ]
    for etiqueta, valor in campos:
        pdf.set_font("Arial", "B", 9)
        pdf.cell(30, 6, etiqueta, 0, 0, "L")
        pdf.set_font("Arial", "", 9)
        pdf.multi_cell(ancho - 30, 6, str(valor), 0, "L")

    pdf.ln(4)

    # ---------- TABLA DE ITEMS ----------
    # Anchos de columna (suman ~190mm)
    w_sku, w_desc, w_cant, w_precio, w_total = 30, 90, 20, 25, 25

    pdf.set_fill_color(*AZUL)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", "B", 8)
    pdf.cell(w_sku, 8, "CODIGO / SKU", 1, 0, "C", fill=True)
    pdf.cell(w_desc, 8, "DESCRIPCION", 1, 0, "C", fill=True)
    pdf.cell(w_cant, 8, "CANTIDAD", 1, 0, "C", fill=True)
    pdf.cell(w_precio, 8, "PRECIO UNITARIO", 1, 0, "C", fill=True)
    pdf.cell(w_total, 8, "TOTAL", 1, 1, "C", fill=True)

    pdf.set_text_color(*NEGRO)
    pdf.set_font("Arial", "", 8)
    for item in cot.items:
        # La descripción puede ser larga: recorto para que quepa en una línea
        desc = item.nombre_producto
        while pdf.get_string_width(desc) > w_desc - 2 and len(desc) > 4:
            desc = desc[:-1]
        pdf.cell(w_sku, 7, item.sku, 1, 0, "L")
        pdf.cell(w_desc, 7, desc, 1, 0, "L")
        pdf.cell(w_cant, 7, str(item.cantidad), 1, 0, "C")
        pdf.cell(w_precio, 7, _dinero(item.precio_unitario), 1, 0, "R")
        pdf.cell(w_total, 7, _dinero(item.total_linea), 1, 1, "R")

    pdf.ln(4)

    # ---------- BLOQUE DE TOTALES (derecha) ----------
    w_lbl, w_val = 40, 30
    x_tot = pdf.l_margin + ancho - (w_lbl + w_val)
    filas_tot = [
        ("Sub-Total:", _dinero(cot.subtotal), False),
        ("Costo del Envio:", _dinero(cot.costo_envio), False),
        ("IVA (16%):", _dinero(cot.iva), False),
        ("TOTAL:", _dinero(cot.total), True),
    ]
    for etiqueta, valor, resaltar in filas_tot:
        pdf.set_x(x_tot)
        if resaltar:
            pdf.set_fill_color(*GRIS)
            pdf.set_font("Arial", "B", 9)
        else:
            pdf.set_fill_color(255, 255, 255)
            pdf.set_font("Arial", "", 9)
        pdf.cell(w_lbl, 7, etiqueta, 1, 0, "R", fill=True)
        pdf.cell(w_val, 7, valor, 1, 1, "R", fill=True)

    pdf.ln(6)

    # ---------- TÉRMINOS Y CONDICIONES ----------
    pdf.set_font("Arial", "B", 10)
    pdf.cell(0, 6, "TERMINOS Y CONDICIONES", 0, 1, "L")
    pdf.set_font("Arial", "", 9)
    terminos = [
        f"1. FORMA DE PAGO: {cot.forma_pago}",
        "2. COTIZACION EN: PESO MEXICANO (MXN)",
        "3. PRECIOS SUJETOS A CAMBIO SIN PREVIO AVISO.",
        f"4. COMENTARIOS: {cot.comentarios}",
    ]
    for t in terminos:
        pdf.multi_cell(ancho, 6, t, 0, "L")

    # ---------- SALIDA BINARIA ----------
    # fpdf 1.7.2: output(dest='S') devuelve str latin-1; lo pasamos a bytes.
    salida = pdf.output(dest="S")
    if isinstance(salida, str):
        return salida.encode("latin-1")
    return bytes(salida)
