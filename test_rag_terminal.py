import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from ingestion.retrieval import retrieve


def main():
    print("RAG Terminal Tester")
    print("Type 'exit' to quit.\n")

    while True:
        question = input("You: ").strip()

        if question.lower() == "exit":
            break

        if not question:
            continue

        try:
            results = retrieve(question)

            print("\nRetrieved information:")
            print(results)
            print("-" * 60)

        except Exception as e:
            print("Error:", e)


if __name__ == "__main__":
    main()