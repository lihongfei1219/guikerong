def main() -> None:
    import uvicorn
    from framex.config import settings

    from guikerong_finance.application import create_app

    uvicorn.run(create_app(), host=settings.server.host, port=settings.server.port, loop="asyncio")


if __name__ == "__main__":
    main()
