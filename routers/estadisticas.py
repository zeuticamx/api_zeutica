import mysql.connector
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
import os
from dotenv import load_dotenv
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

router =APIRouter(tags=["/estadisticas"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class fechas(BaseModel):
    fecha: str
    fecha2: str

@router.post('/obtener-estadistica')
async def obtener_estadistica(fecha: fechas):
    """
    Consulta y aplica Modelos de ciencia de datos para obtener una proyeccion
    de venta semanal por sku.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = "SELECT id_ventas, sku, producto, cantidad, precio, costo_unitario, fecha, nombreComprador, plataforma, condicion_pago FROM ventasRegistro WHERE estatus = 'activa' AND DATE(fecha) BETWEEN %s AND %s"
        cursor.execute(query,(fecha.fecha, fecha.fecha2))
        ventas = cursor.fetchall()

        if not ventas:
            raise HTTPException(status_code=404, detail="No se han encontrado registro de ventas")

        # Cargar datos de MySQL a un DataFrame de Pandas
        df = pd.DataFrame(ventas)
        df['fecha'] = pd.to_datetime(df['fecha'])

        # Agrupar por SKU y por día para tener una serie limpia
        df_venta = df.groupby(['sku', pd.Grouper(key='fecha', freq='D')]).sum().reset_index()

        # Extraer variables temporales
        df_venta['mes'] = df_venta['fecha'].dt.month
        df_venta['dia_semana'] = df_venta['fecha'].dt.dayofweek
        df_venta['dia_mes'] = df_venta['fecha'].dt.day

        # Crear "Lags" (Ventas pasadas)
        # Esto le dice al modelo: "mira cuánto se vendió ayer y hace 7 días"
        df_venta = df_venta.sort_values(['sku', 'fecha'])
        df_venta['ventas_ayer'] = df_venta.groupby('sku')['cantidad'].shift(1)
        df_venta['ventas_hace_7dias'] = df_venta.groupby('sku')['cantidad'].shift(7)

        # Media Móvil (Suaviza la tendencia)
        df_venta['media_7_dias'] = df_venta.groupby('sku')['cantidad'].transform(lambda x: x.rolling(window=7).mean())

        # Limpiar valores nulos creados por los shifts/rolling
        df_venta = df_venta.fillna(0)

        # Definir variables de entrada (X) y objetivo (y)
        features = ['mes', 'dia_semana', 'dia_mes', 'ventas_ayer', 'ventas_hace_7dias', 'media_7_dias']
        X = df_venta[features]
        y = df_venta['cantidad']

        # Configurar y entrenar el modelo
        # n_estimators=100 es un buen estándar para empezar
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X, y)

        # Realizar predicción por cada SKU
        # Aquí tomamos la última fila de cada SKU para proyectar el futuro
        ultimos_datos = df_venta.groupby('sku').tail(1).copy()
        predicciones = model.predict(ultimos_datos[features])

        # Añadir predicción al resultado
        ultimos_datos['prediccion_ventas'] = predicciones

        # Preparar datos para el UPDATE
        # Supongamos que tu columna se llama 'prediccion_stock'
        datos_update = [
            (row['prediccion_ventas'], row['sku']) 
            for _, row in ultimos_datos.iterrows()
        ]

        update_query = "UPDATE productos SET prediccion_stock = %s WHERE sku = %s"
        cursor.executemany(update_query, datos_update)
        conn.commit()

        # Retorno las predicciones para que el cliente sepa qué se guardó
        resultado = ultimos_datos[['sku', 'cantidad', 'prediccion_ventas']].to_dict(orient='records')
        return {"msg": "Predicciones actualizadas", "predicciones": resultado}

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close()