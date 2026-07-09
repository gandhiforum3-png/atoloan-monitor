"""
=============================================================
 INTRODUCTORY LANGCHAIN AGENT — How LLM Agents Work
=============================================================

WHAT IS AN AGENT?
-----------------
A regular LLM call is one-shot: you send a prompt, you get a response.
An *agent* is different — it can:
  1. Decide WHICH tool (function) to use to answer a question
  2. CALL that tool and observe the result
  3. Repeat (think → act → observe) until it has a final answer

This loop is called the ReAct pattern:
  Reason → Act → Observe → Reason → Act → Observe → ...

KEY COMPONENTS
--------------
  LLM          — the brain (e.g. GPT-4o via OpenAI)
  Tools        — functions the agent can call (search, calculator, etc.)
  Agent        — the logic that decides what to do next
  AgentExecutor— runs the think/act/observe loop until done

SETUP
-----
Install dependencies:
    pip install langchain langchain-openai

Set your API key:
    export OPENAI_API_KEY="sk-..."

Then run:
    python langchain_intro_agent.py
"""

import os
from langchain_openai import ChatOpenAI
from langchain_classic.agents import AgentExecutor, create_react_agent
from langchain.tools import tool
from langchain_classic import hub

# ─────────────────────────────────────────────
# STEP 1 — Define Tools
#
# A tool is just a Python function decorated with @tool.
# The docstring is critical — the LLM reads it to decide
# when and how to use the tool.
# ─────────────────────────────────────────────

@tool
def calculator(expression: str) -> str:
    """
    Evaluates a basic math expression and returns the result.
    Use this whenever you need to do arithmetic.
    Examples: '2 + 2', '100 * 3.14', '(5 ** 2) / 2'
    """
    try:
        # eval() is fine here since it's a demo; in production, use a safe parser
        result = eval(expression, {"__builtins__": {}})
        return f"{expression} = {result}"
    except Exception as e:
        return f"Error evaluating '{expression}': {e}"


@tool
def word_counter(text: str) -> str:
    """
    Counts the number of words in the given text.
    Use this when someone asks how many words are in a sentence or paragraph.
    """
    count = len(text.split())
    return f"The text has {count} word(s)."


@tool
def reverse_string(text: str) -> str:
    """
    Reverses a given string character by character.
    Use this when someone asks to reverse a word or sentence.
    """
    return f"Reversed: '{text[::-1]}'"


# Collect all tools in a list — the agent will have access to all of them
tools = [calculator, word_counter, reverse_string]


# ─────────────────────────────────────────────
# STEP 2 — Set Up the LLM
#
# This is the "brain". We use GPT-4o-mini here because it's
# fast and cheap — great for learning.
# temperature=0 makes responses deterministic (less random).
# ─────────────────────────────────────────────

llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
)


# ─────────────────────────────────────────────
# STEP 3 — Load a Prompt Template
#
# The ReAct prompt template tells the LLM HOW to think:
#   "You have these tools. To answer a question, think step
#    by step. Use a tool if needed. When done, say Final Answer."
#
# We pull a standard one from LangChain Hub — no need to
# write it yourself when learning.
# ─────────────────────────────────────────────

prompt = hub.pull("hwchase17/react")
# Curious what this prompt looks like? Print it:
# print(prompt.template)


# ─────────────────────────────────────────────
# STEP 4 — Create the Agent
#
# create_react_agent wires together:
#   - the LLM (the brain)
#   - the tools (the hands)
#   - the prompt (the instructions)
#
# The result is an agent *runnable* — it knows the ReAct loop
# but doesn't run it yet.
# ─────────────────────────────────────────────

agent = create_react_agent(llm=llm, tools=tools, prompt=prompt)


# ─────────────────────────────────────────────
# STEP 5 — Create the AgentExecutor
#
# The AgentExecutor is the loop runner.
# It repeatedly:
#   1. Asks the agent "what to do next?"
#   2. Runs the chosen tool
#   3. Feeds the result back to the agent
# Until the agent says "Final Answer: ..."
#
# verbose=True prints each Thought / Action / Observation
# so you can SEE the reasoning in real time — highly
# recommended when learning!
# ─────────────────────────────────────────────

agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,          # Show the reasoning steps
    max_iterations=5,      # Safety: stop after 5 steps max
    handle_parsing_errors=True,
)


# ─────────────────────────────────────────────
# STEP 6 — Run the Agent
#
# Now ask it something! The agent will figure out which
# tools to use (possibly multiple) to answer.
# ─────────────────────────────────────────────

def run(question: str):
    print("\n" + "=" * 60)
    print(f"QUESTION: {question}")
    print("=" * 60)
    result = agent_executor.invoke({"input": question})
    print("\n✅ FINAL ANSWER:", result["output"])
    print("=" * 60 + "\n")


if __name__ == "__main__":

    # Example 1: Uses the calculator tool
    run("What is 137 multiplied by 48?")

    # Example 2: Uses the word_counter tool
    run("How many words are in the sentence: 'The quick brown fox jumps over the lazy dog'?")

    # Example 3: Uses the reverse_string tool
    run("Can you reverse the word 'LangChain'?")

    # Example 4: Multi-step — uses calculator twice
    run("If I have 25 apples and triple that, then subtract 18, how many do I have?")

    # Try your own! Uncomment and edit:
    # run("Your question here")
