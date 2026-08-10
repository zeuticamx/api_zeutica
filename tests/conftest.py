# Asegura que la raiz de api_zeutica1 este en sys.path para poder
# importar "routers.embarques", "mov_reg" y "banxico_service" sin
# depender de como se invoque pytest (pytest vs python -m pytest).
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)
