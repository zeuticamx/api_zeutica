from fastapi import APIRouter, HTTPException, status, Request, Depends
import asyncpg

router = APIRouter(tags=["sofi-conversaciones"], responses={404: {"Mensaje": "No encontrado"}})

async def get_db_postgres(request: Request):
    """
    Toma el pool de Postgres desde main.py
    """
    if not hasattr(request.app.state, 'db_pool') or request.app.state.db_pool is None:
         raise HTTPException(status_code=500, detail="El pool de Postgres no está inicializado")
         
    async with request.app.state.db_pool.acquire() as connection:
        yield connection

@router.get("/sofi-conversaciones")
async def obtener_conversaciones(db: asyncpg.Connection = Depends(get_db_postgres)):
    try:    
        rows = await db.fetch("SELECT session_id, message FROM n8n_chat_histories")
        conversaciones = [dict(row) for row in rows]
        return {"conversaciones": conversaciones}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error en la base de datos PostgreSQL: {str(e)}"
        )