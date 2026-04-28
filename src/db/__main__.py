from .queries import initialize_database


def main() -> None:
    initialize_database()
    print("Database initialized.")


if __name__ == "__main__":
    main()
