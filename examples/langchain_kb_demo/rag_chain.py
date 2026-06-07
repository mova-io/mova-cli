"""Minimal RAG chain using MdkRetriever + LangGraph.

    python examples/langchain_kb_demo/rag_chain.py \
        --agent demo-support \
        --runtime-url https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io \
        --question "What does the standard plan include?"
"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG chain with mdk KB + LangGraph")
    parser.add_argument("--agent", required=True, help="Agent name with KB data")
    parser.add_argument("--runtime-url", required=True, help="mdk runtime URL")
    parser.add_argument("--api-key", default=os.environ.get("MDK_DEV_KEY", ""), help="API key")
    parser.add_argument("--question", required=True, help="Question to answer")
    parser.add_argument("--top-k", type=int, default=5, help="Number of KB chunks to retrieve")
    args = parser.parse_args()

    from langchain_openai import ChatOpenAI  # noqa: PLC0415
    from langgraph.graph import END, START, StateGraph  # noqa: PLC0415

    from movate.integrations.langchain_retriever import MdkRetriever  # noqa: PLC0415

    # Build the retriever.
    retriever = MdkRetriever(
        agent_name=args.agent,
        runtime_url=args.runtime_url,
        api_key=args.api_key,
        top_k=args.top_k,
    )
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    # Retrieve step: query KB, join chunks into context.
    def retrieve(state: dict) -> dict:
        docs = retriever.invoke(state["question"])
        context = "\n\n".join(
            f"[{d.metadata.get('score', 0):.2f}] {d.page_content}" for d in docs
        )
        print(f"\n--- Retrieved {len(docs)} chunks ---")
        for d in docs:
            print(f"  [{d.metadata.get('score', 0):.2f}] {d.page_content[:80]}...")
        return {**state, "context": context}

    # Answer step: LLM generates answer grounded in context.
    def answer(state: dict) -> dict:
        prompt = (
            f"You are a helpful product support agent. Answer the question using "
            f"ONLY the context below. If the context doesn't contain the answer, "
            f"say so.\n\n"
            f"Context:\n{state['context']}\n\n"
            f"Question: {state['question']}\n\n"
            f"Answer:"
        )
        response = llm.invoke(prompt)
        return {**state, "answer": response.content}

    # Build the graph.
    builder = StateGraph(dict)
    builder.add_node("retrieve", retrieve)
    builder.add_node("answer", answer)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "answer")
    builder.add_edge("answer", END)
    graph = builder.compile()

    # Run it.
    print(f"\nQuestion: {args.question}")
    result = graph.invoke({"question": args.question})
    print(f"\nAnswer: {result['answer']}")


if __name__ == "__main__":
    main()
