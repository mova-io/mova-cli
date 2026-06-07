# LangChain KB Retriever Demo

Demonstrates the `MdkRetriever` — mdk's knowledge base exposed as a
LangChain `BaseRetriever`. LangGraph agents can query mdk's KB natively.

## Prerequisites

```bash
# Install the langchain extra
uv tool install --editable '.[langchain]' --force

# Have a running mdk runtime with an agent that has KB data
mdk serve --dev
```

## Usage — Remote mode (against a deployed runtime)

```python
from movate.integrations.langchain_retriever import MdkRetriever

# Connect to a deployed mdk runtime
retriever = MdkRetriever(
    agent_name="demo-support",
    runtime_url="https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io",
    api_key="mvt_live_...",
    top_k=5,
)

# Query the KB — returns LangChain Document objects
docs = retriever.invoke("What does the standard plan include?")
for doc in docs:
    print(f"[{doc.metadata.get('score', 0):.2f}] {doc.page_content[:100]}...")
```

## Usage — In a LangGraph RAG chain

```python
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from movate.integrations.langchain_retriever import MdkRetriever

retriever = MdkRetriever(agent_name="demo-support", runtime_url="https://...")
llm = ChatOpenAI(model="gpt-4o-mini")

# Build a simple RAG graph
def retrieve(state):
    docs = retriever.invoke(state["question"])
    return {**state, "context": "\n".join(d.page_content for d in docs)}

def answer(state):
    prompt = f"Context:\n{state['context']}\n\nQuestion: {state['question']}\nAnswer:"
    response = llm.invoke(prompt)
    return {**state, "answer": response.content}

builder = StateGraph(dict)
builder.add_node("retrieve", retrieve)
builder.add_node("answer", answer)
builder.add_edge(START, "retrieve")
builder.add_edge("retrieve", "answer")
builder.add_edge("answer", END)

graph = builder.compile()
result = graph.invoke({"question": "What does the standard plan include?"})
print(result["answer"])
```

## Usage — Voice agent with KB grounding

```python
from movate.integrations.deep_agents import voice_deep_agent
from movate.integrations.langchain_retriever import MdkRetriever

retriever = MdkRetriever(agent_name="demo-support", runtime_url="https://...")

def search_kb(query: str) -> str:
    """Search the product knowledge base."""
    docs = retriever.invoke(query)
    return "\n".join(d.page_content for d in docs[:3])

# Voice agent with KB tool
turn = voice_deep_agent(
    model="openai:gpt-4o-mini",
    system_prompt="You are a product support agent. Use the search_kb tool for accurate answers.",
    tools=[search_kb],
)
```

## How it works

`MdkRetriever` wraps mdk's KB search as a standard LangChain retriever:

1. **Remote mode**: `POST /api/v1/agents/{name}/kb/search` with `{"question": query, "k": top_k}`
2. **Local mode**: Direct `StorageProvider` call (same as `mdk run` uses internally)
3. Each KB chunk → `Document(page_content=text, metadata={score, chunk_id, source})`
4. Works with any LangChain/LangGraph component that accepts a `BaseRetriever`
