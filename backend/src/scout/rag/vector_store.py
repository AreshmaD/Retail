from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

POLICY_DOCS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "policies"
PERSIST_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chroma"
EMBEDDING_MODEL = "nomic-embed-text"
COLLECTION_NAME = "policy_docs"


def _get_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(model=EMBEDDING_MODEL)


def _load_policy_documents() -> list[Document]:
    """Reads each policy .md file directly into a Document — no document
    loader dependency needed for plain text files."""
    docs = []
    for file_path in sorted(POLICY_DOCS_DIR.glob("*.md")):
        text = file_path.read_text(encoding="utf-8")
        docs.append(Document(page_content=text, metadata={"source": file_path.name}))
    return docs


def build_vector_store() -> Chroma:
    """Loads all policy .md files, chunks them, and embeds them into Chroma.
    For a clean rebuild, delete data/chroma/ first."""
    docs = _load_policy_documents()

    splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=50)
    chunks = splitter.split_documents(docs)

    vector_store = Chroma.from_documents(
        documents=chunks,
        embedding=_get_embeddings(),
        collection_name=COLLECTION_NAME,
        persist_directory=str(PERSIST_DIR),
    )
    return vector_store


def load_vector_store() -> Chroma:
    """Loads the existing persisted Chroma collection without re-embedding."""
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=_get_embeddings(),
        persist_directory=str(PERSIST_DIR),
    )


def retrieve_policy_chunks(query: str, k: int = 3) -> list[dict]:
    """Retrieves the top-k most relevant policy chunks for a query."""
    vector_store = load_vector_store()
    results = vector_store.similarity_search(query, k=k)
    return [
        {"content": doc.page_content, "source": doc.metadata.get("source", "unknown")}
        for doc in results
    ]


if __name__ == "__main__":
    build_vector_store()
    print(f"Vector store built at {PERSIST_DIR}")
