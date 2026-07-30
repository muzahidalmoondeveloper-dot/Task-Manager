import asyncio

from app.services.ai_task_extractor import AITaskExtractor


async def main():
    extractor = AITaskExtractor()

    tasks, raw = await extractor.extract_tasks(
        source_type="email",
        source_title="Website launch",
        source_text="""
        Hi Alana,
        Please prepare the homepage content by Friday.
        Muzahid will review the design tomorrow.
        Thanks.
        """,
    )

    print(raw)

    for task in tasks:
        print(task)


if __name__ == "__main__":
    asyncio.run(main())