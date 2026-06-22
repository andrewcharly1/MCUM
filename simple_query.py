import os
import psycopg
from psycopg.rows import dict_row

def main():
    env_file = r"C:\Users\dev\workspace\.agent\skills\MCUM\.env"
    try:
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ[key] = val.strip('\"').strip("\'")
    except Exception as e:
        print("No se pudo leer el .env. Error:", e)

    db_name = os.environ.get("DB_NAME", "postgres")
    db_user = os.environ.get("DB_USER", "postgres")
    db_pw = os.environ.get("DB_PASSWORD", "")
    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = os.environ.get("DB_PORT", "5432")
    
    conn_str = f"host={db_host} port={db_port} dbname={db_name} user={db_user} password='{db_pw}'"
    try:
        conn = psycopg.connect(conn_str, row_factory=dict_row)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    pl.title,
                    pl.skill_used,
                    pl.outcome,
                    p.project_name,
                    pl.created_at
                FROM project_registry.project_logs pl
                LEFT JOIN project_registry.projects p ON pl.project_id = p.id
                WHERE pl.created_at >= CURRENT_DATE
                ORDER BY pl.created_at DESC
            """)
            rows = cur.fetchall()
            print(f"Total Logs Hoy: {len(rows)}")
            for r in rows:
                print(f"[{r['created_at']}] Proyecto: {r['project_name']} | Skill Delegado: {r['skill_used']}")
                print(f"  Éxito: {r['outcome']}")
                print(f"  Descripción: {r['title']}")
                print("-" * 50)
                
            # Verificar si hay webscraping registrado en todo el historial
            print("--- BUSCANDO ACTIVIDADES DE WEBSCRAPING O DIRECCIÓN DEL TRABAJO ---")
            cur.execute("""
                SELECT 
                    pl.title, pl.created_at, pl.skill_used
                FROM project_registry.project_logs pl
                WHERE title ILIKE '%scrap%' 
                   OR title ILIKE '%direccion del trabajo%'
                   OR title ILIKE '%dirección del trabajo%'
                ORDER BY created_at DESC LIMIT 5
            """)
            scrap_rows = cur.fetchall()
            print(f"Encontrados: {len(scrap_rows)}")
            for r in scrap_rows:
                print(f"  [{r['created_at']}] (Skill: {r['skill_used']}) -> {r['title'][:100]}")
    except Exception as e:
        print("ERROR CONECTANDO/CONSULTANDO:", e)

if __name__ == '__main__':
    main()
