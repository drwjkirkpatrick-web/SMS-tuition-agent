#!/usr/bin/env python3
"""
scripts/sync_sis.py — Manual SIS sync trigger
═══════════════════════════════════════════════════

Run this script inside the API container to force a SIS sync:
  docker compose exec api python -m scripts.sync_sis
"""
import asyncio
from adapters.connector_factory import get_connector
from adapters.csv_connector import persist_guardians, persist_students
from adapters.sis_connector import SyncCheckpoint
from domain.models import School
from infra.database import async_session_factory
from sqlalchemy import select


async def main():
    async with async_session_factory() as session:
        result = await session.execute(select(School).where(School.id == 1))
        school = result.scalar_one_or_none()
        if not school:
            print("School not found")
            return
        
        connector = get_connector(
            school_id=school.id,
            adapter_type=school.sis_adapter_type or "csv",
            config={"csv_directory": "/data/sis_exports"},
        )
        if not connector:
            print(f"No connector for type: {school.sis_adapter_type}")
            return
        
        checkpoint = await connector.get_checkpoint()
        print(f"Last sync: {checkpoint.last_sync_at}")
        
        # Sync students
        students = await connector.sync_students(checkpoint)
        count = await persist_students(session, school.id, students)
        print(f"Synced {count} students")
        
        # Sync guardians
        guardians = await connector.sync_guardians(checkpoint)
        count = await persist_guardians(session, school.id, guardians)
        print(f"Synced {count} guardians")
        
        await session.commit()
        print("Sync complete")


if __name__ == "__main__":
    asyncio.run(main())
