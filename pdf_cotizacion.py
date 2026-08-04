"""
Generador de PDF de cotizaciones para Zeutica.

Reproduce el formato del documento ejemplo (cotizacion_prueba.pdf):
- Encabezado con logo, datos de la empresa y título COTIZACION ZTC-xxx
- Tabla Fecha / Válido Hasta a la derecha
- Banner CLIENTE con sus datos
- Tabla de items (SKU, descripción multilínea, cantidad, precio, total)
- Bloque de totales (Sub-Total, Costo del Envío, IVA 16%, TOTAL)
- Términos y condiciones (forma de pago, método de pago, moneda, comentarios)
- Recuadro DATOS BANCARIOS
- Pie de página con contacto y número de página

Depende solo de fpdf 1.7.2 (pyfpdf), que ya está en requirements.
La función principal `generar_pdf_cotizacion` devuelve los bytes del PDF.
"""
import os
from datetime import datetime
from fpdf import FPDF

# Paleta y datos fijos de la empresa
AZUL = (31, 78, 121)        # azul corporativo (banners / encabezados de tabla)
GRIS = (230, 230, 230)      # gris claro para la fila TOTAL
NEGRO = (0, 0, 0)
BLANCO = (255, 255, 255)

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "imagenes", "logo.png")

EMPRESA_DOMICILIO = "Domicilio: Blvd De los Charros 1629 Belenes Norte Cp 45145, Zapopan, Jalisco."
EMPRESA_WEB = "www.zeutica.com"
EMPRESA_TEL = "Teléfono: 33-1299-5688"
EMPRESA_EMAIL = "E-mail: ventas1@zeutica.com"
VALIDO_HASTA = "7 Días"

METODO_PAGO_DEFAULT = "PUE - PAGO EN UNA SOLA EXHIBICIÓN"

BANCO_CLABE = "002320702110604152"
BANCO_NOMBRE_BANCO = "Banamex"
BANCO_TITULAR = "Felipe Osvaldo Ruvalcaba Ayala"


def _dinero(valor) -> str:
    """Formatea un número como moneda: 8700 -> $8,700.00"""
    return f"${float(valor):,.2f}"


def _txt(valor) -> str:
    """Texto seguro para fpdf (latin-1); None -> cadena vacía."""
    if valor is None:
        return ""
    return str(valor).encode("latin-1", "replace").decode("latin-1")


class _CotizacionPDF(FPDF):
    """PDF con pie de página fijo replicando el ejemplo."""

    def footer(self):
        self.set_y(-25)
        self.set_font("Arial", "B", 8)
        self.set_text_color(*NEGRO)
        self.cell(0, 5, "Si tienes alguna pregunta por favor contáctanos", 0, 1, "C")
        self.set_font("Arial", "", 8)
        self.cell(0, 5, "Tel: 33-1299-5688 / E-mail: ventas1@zeutica.com", 0, 1, "C")
        self.set_y(-12)
        self.set_font("Arial", "", 8)
        self.cell(0, 5, f"Página {self.page_no()}", 0, 0, "R")


def _fila_item(pdf, anchos, textos, alineaciones, alto_linea=6):
    """
    Dibuja una fila de la tabla de items permitiendo que la descripción
    ocupe varias líneas; todas las celdas de la fila crecen a la misma altura.
    """
    # Cuántas líneas necesita cada celda con su ancho disponible
    lineas_max = 1
    for w, texto in zip(anchos, textos):
        lineas = 1
        actual = ""
        for palabra in texto.split(" "):
            prueba = (actual + " " + palabra).strip()
            if pdf.get_string_width(prueba) > w - 3:
                lineas += 1
                actual = palabra
            else:
                actual = prueba
        lineas_max = max(lineas_max, lineas)
    alto = alto_linea * lineas_max

    # Salto de página manual si la fila no cabe
    if pdf.get_y() + alto > pdf.page_break_trigger:
        pdf.add_page()

    x = pdf.l_margin
    y = pdf.get_y()
    for w, texto, alin in zip(anchos, textos, alineaciones):
        pdf.rect(x, y, w, alto)
        pdf.set_xy(x, y)
        pdf.multi_cell(w, alto_linea, texto, 0, alin)
        x += w
    pdf.set_xy(pdf.l_margin, y + alto)


def generar_pdf_cotizacion(cot) -> bytes:
    """
    Construye el PDF de la cotización y devuelve su contenido binario (bytes).

    `cot` es el objeto CotizacionSchema (o cualquiera con los mismos atributos:
    codigo_cotizacion, empresa, atencion, email, domicilio, telefono, subtotal,
    iva, total, costo_envio, forma_pago, metodo_pago, comentarios, usuario, items).
    """
    pdf = _CotizacionPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=30)
    pdf.add_page()
    ancho = pdf.w - pdf.l_margin - pdf.r_margin  # ancho útil (~190mm)

    # ---------- ENCABEZADO ----------
    y_top = pdf.get_y()

    # Logo (1024x341 -> proporción ~3:1); si falta el archivo, texto de respaldo
    if os.path.exists(LOGO_PATH):
        pdf.image(LOGO_PATH, x=pdf.l_margin, y=y_top, w=50)
    else:
        pdf.set_font("Arial", "B", 28)
        pdf.set_text_color(*AZUL)
        pdf.text(pdf.l_margin, y_top + 12, "Zeutica")

    # Título a la derecha
    pdf.set_xy(pdf.l_margin + 90, y_top)
    pdf.set_font("Arial", "B", 18)
    pdf.set_text_color(*NEGRO)
    pdf.cell(ancho - 90, 10, _txt(f"COTIZACION {cot.codigo_cotizacion}"), 0, 1, "R")

    # Tabla Fecha / Válido Hasta (derecha, debajo del título)
    x_tabla = pdf.l_margin + ancho - 70
    fecha_hoy = datetime.now().strftime("%d/%m/%Y")
    pdf.set_xy(x_tabla, y_top + 14)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(30, 7, "Fecha:", 1, 0, "L")
    pdf.set_font("Arial", "", 9)
    pdf.cell(40, 7, fecha_hoy, 1, 1, "R")
    pdf.set_x(x_tabla)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(30, 7, "Válido Hasta:", 1, 0, "L")
    pdf.set_font("Arial", "", 9)
    pdf.cell(40, 7, VALIDO_HASTA, 1, 1, "R")

    # Datos de contacto de la empresa (izquierda, debajo del logo)
    pdf.set_xy(pdf.l_margin, y_top + 20)
    pdf.set_font("Arial", "", 8)
    pdf.set_text_color(*NEGRO)
    for linea in (EMPRESA_DOMICILIO, EMPRESA_WEB, EMPRESA_TEL, EMPRESA_EMAIL,
                  f"Asesor: {_txt(cot.usuario)}"):
        pdf.cell(115, 4.5, linea, 0, 1, "L")

    pdf.ln(8)

    # ---------- BANNER CLIENTE ----------
    pdf.set_fill_color(*AZUL)
    pdf.set_text_color(*BLANCO)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(ancho, 8, "  CLIENTE", 0, 1, "L", fill=True)
    pdf.ln(1)

    # Datos del cliente (valores en mayúsculas como el ejemplo)
    pdf.set_text_color(*NEGRO)
    campos = [
        ("NOMBRE:", cot.atencion),
        ("EMPRESA:", cot.empresa),
        ("EMAIL:", cot.email),
        ("DOMICILIO:", cot.domicilio),
        ("TELÉFONO:", cot.telefono),
    ]
    for etiqueta, valor in campos:
        pdf.set_font("Arial", "B", 9)
        pdf.cell(30, 6, etiqueta, 0, 0, "L")
        pdf.set_font("Arial", "", 9)
        pdf.multi_cell(ancho - 30, 6, _txt(valor).upper(), 0, "L")

    pdf.ln(4)

    # ---------- TABLA DE ITEMS ----------
    # Anchos de columna (suman ~190mm)
    w_sku, w_desc, w_cant, w_precio, w_total = 30, 90, 20, 25, 25
    anchos = (w_sku, w_desc, w_cant, w_precio, w_total)

    pdf.set_fill_color(*AZUL)
    pdf.set_text_color(*BLANCO)
    pdf.set_font("Arial", "B", 8)
    pdf.cell(w_sku, 8, "CÓDIGO / SKU", 1, 0, "C", fill=True)
    pdf.cell(w_desc, 8, "DESCRIPCIÓN", 1, 0, "C", fill=True)
    pdf.cell(w_cant, 8, "CANTIDAD", 1, 0, "C", fill=True)
    pdf.cell(w_precio, 8, "PRECIO UNITARIO", 1, 0, "C", fill=True)
    pdf.cell(w_total, 8, "TOTAL", 1, 1, "C", fill=True)

    pdf.set_text_color(*NEGRO)
    pdf.set_font("Arial", "", 8)
    for item in cot.items:
        _fila_item(
            pdf,
            anchos,
            (
                _txt(item.sku),
                _txt(item.nombre_producto).upper(),
                str(item.cantidad),
                _dinero(item.precio_unitario),
                _dinero(item.total_linea),
            ),
            ("L", "L", "C", "R", "R"),
        )

    pdf.ln(4)

    # ---------- BLOQUE DE TOTALES (derecha) ----------
    w_lbl, w_val = 40, 30
    x_tot = pdf.l_margin + ancho - (w_lbl + w_val)
    filas_tot = [
        ("Sub-Total:", _dinero(cot.subtotal), False),
        ("Costo del Envío:", _dinero(cot.costo_envio), False),
        ("IVA (16%):", _dinero(cot.iva), False),
        ("TOTAL:", _dinero(cot.total), True),
    ]
    for etiqueta, valor, resaltar in filas_tot:
        pdf.set_x(x_tot)
        if resaltar:
            pdf.set_fill_color(*GRIS)
            pdf.set_font("Arial", "B", 9)
        else:
            pdf.set_fill_color(*BLANCO)
            pdf.set_font("Arial", "", 9)
        pdf.cell(w_lbl, 7, etiqueta, 1, 0, "R", fill=True)
        pdf.cell(w_val, 7, valor, 1, 1, "R", fill=True)

    pdf.ln(6)

    # ---------- TÉRMINOS Y CONDICIONES ----------
    metodo_pago = getattr(cot, "metodo_pago", None) or METODO_PAGO_DEFAULT
    pdf.set_font("Arial", "B", 10)
    pdf.cell(0, 6, "TÉRMINOS Y CONDICIONES", 0, 1, "L")
    pdf.set_font("Arial", "", 9)
    terminos = [
        f"1. FORMA DE PAGO: {_txt(cot.forma_pago).upper()}",
        f"2. MÉTODO DE PAGO: {_txt(metodo_pago).upper()}",
        "3. COTIZACIÓN EN: PESO MEXICANO (MXN)",
        "4. PRECIOS SUJETOS A CAMBIO SIN PREVIO AVISO.",
        f"5. COMENTARIOS: {_txt(cot.comentarios).upper()}",
    ]
    for t in terminos:
        pdf.multi_cell(ancho, 6, t, 0, "L")

    pdf.ln(6)

    # ---------- DATOS BANCARIOS ----------
    lineas_banco = [
        f"Clabe Interbancaria: {BANCO_CLABE}",
        f"Banco: {BANCO_NOMBRE_BANCO}",
        f"Nombre: {BANCO_TITULAR}",
    ]
    alto_caja = 7 + len(lineas_banco) * 6 + 3
    if pdf.get_y() + alto_caja > pdf.page_break_trigger:
        pdf.add_page()
    x_caja = pdf.l_margin
    y_caja = pdf.get_y()
    w_caja = 110
    pdf.set_draw_color(*AZUL)
    pdf.set_line_width(0.5)
    pdf.rect(x_caja, y_caja, w_caja, alto_caja)
    pdf.set_line_width(0.2)
    pdf.set_draw_color(*NEGRO)
    pdf.set_xy(x_caja + 3, y_caja + 2)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(0, 6, "DATOS BANCARIOS:", 0, 1, "L")
    pdf.set_font("Arial", "", 9)
    for linea in lineas_banco:
        pdf.set_x(x_caja + 3)
        pdf.cell(0, 6, linea, 0, 1, "L")

    # ---------- SALIDA BINARIA ----------
    # fpdf 1.7.2: output(dest='S') devuelve str latin-1; lo pasamos a bytes.
    salida = pdf.output(dest="S")
    if isinstance(salida, str):
        return salida.encode("latin-1")
    return bytes(salida)
