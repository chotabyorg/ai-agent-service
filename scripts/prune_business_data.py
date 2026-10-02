"""Run only against the explicitly configured service database after rollout approval."""
import asyncio
from db.database import AsyncSessionLocal
from business_capabilities.maintenance import prune_business_data


async def main():
    async with AsyncSessionLocal() as db:
        result = await prune_business_data(db)
        print(result)


if __name__ == '__main__':
    asyncio.run(main())
