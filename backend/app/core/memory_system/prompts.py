"""记忆系统 Prompt 模板 - loadMemoryPrompt（标准/TEAMMEM）与各类后台任务的提示词"""
from typing import List, Optional

import structlog
from app.config import settings

logger = structlog.get_logger()


# ===== 记忆选择 side-query Prompt =====

SELECTION_PROMPT = """You are a memory relevance selector. Given the user's current query and a manifest of available memory files, select the files that are clearly relevant.

## Rules
1. STRICT criterion: if you are NOT sure a memory helps with the user query, do NOT select it.
4. Select AT MOST {select_max} files. Prefer fewer, higher-confidence selections.

## User query
{user_query}

## Candidate memories (filename | name | description)
{manifest}

## Output format
Respond with ONLY a JSON array of selected filenames, e.g. ["a.md", "b.md"].
If nothing is relevant, respond with []. Do NOT add any explanation."""


# ===== 记忆提取 Prompt =====

EXTRACTION_PROMPT = """You are a memory extraction agent. Analyze the NEW conversation increment below and decide whether it contains knowledge worth persisting as long-term memory.

## What counts as memory (write it)
- user: user preferences, long-term facts about the user
- feedback: user corrections / feedback to the assistant's behavior
- reference: reference material, external knowledge, links, data sources
- project: project-specific knowledge, decisions, architecture, conventions

## What does NOT count (never write it)
- plan: the current task plan — belongs to the task, not long-term memory
- tasks: short-term todo lists — not long-term memory
- transient chit-chat, greetings, or anything not reusable in future sessions

## Existing memories (manifest)
{manifest}

## New conversation increment (since last extraction)
{increment}

## Instructions
1. Use read_file to inspect existing memory files when you need to merge or avoid duplication.
2. Use write_file to create or update memory files under the memory directory ONLY.
   Each file MUST start with YAML frontmatter:
   ---
   name: <short title>
   description: <one-line description>
   type: user|feedback|reference|project
   scope: personal|team
   ---
   Then the Markdown body.
3. Prefer updating an existing file over creating a duplicate.
4. If there is NOTHING worth recording, respond with exactly: SKIP

Constraint: finish within {max_rounds} tool rounds."""


# ===== 记忆整理/合并 Prompt =====

CONSOLIDATION_PROMPT = """You are a memory consolidation agent. Your job is to merge, deduplicate and prune memory files to keep the memory library compact and high-quality.

## Memory files (manifest)
{manifest}

## Recent conversation signals
{recent_signals}

## Instructions
1. Use read_file to examine memory files that look overlapping or redundant.
2. Merge related content into a single authoritative file with write_file (delete the merged-away facts from other files by rewriting them).
3. Prune stale or contradicted entries (rewrite the file without them).
4. Keep each file focused on ONE topic with valid YAML frontmatter:
   ---
   name: <short title>
   description: <one-line description>
   type: user|feedback|reference|project
   scope: personal|team
   ---
5. If nothing needs consolidation, respond with exactly: SKIP"""


# ===== Session Memory 更新 Prompt =====

SESSION_MEMORY_TEMPLATE = """# Session Memory

## 当前状态
(Current state: what is done, what is in progress)

## 任务说明
(Task description and goals)

## 错误与修正
(Errors encountered and how they were fixed)
"""

SESSION_MEMORY_UPDATE_PROMPT = """You are a session memory note-taker. Maintain a structured Markdown note about the ongoing conversation.

## Current note content
{current_note}

## New conversation increment (since last note update)
{increment}

## Instructions
- The note has sections: "当前状态", "任务说明", "错误与修正".
- ONLY update the sections that changed. Do NOT rewrite unchanged sections.
- Use write_file to save the updated note to: {note_path}
- Keep the note concise and factual. Finish within {max_rounds} tool rounds."""


# ===== loadMemoryPrompt：系统提示词中的记忆教学 =====

_STANDARD_MEMORY_TEACHING = """
## Memory System

You have a dedicated memory directory at `{memory_root}`. The directory ALREADY EXISTS — you can write files into it directly with write_file, without checking or creating it first (saves a round-trip).

### When to WRITE memory
- The user explicitly says "remember this" / "记住这个" → write IMMEDIATELY.
- The user corrects you → record it as feedback so you don't repeat the mistake.
- You learn a durable fact about the user, the project, or a useful reference.

### When to READ memory
- At the start of a task, relevant memories may be injected into the context as system-reminders — use them, but verify freshness (a warning is attached if a memory is older than 1 day).
- You may read `{index_filename}` or specific memory files when you need more detail.

### Memory types (YAML frontmatter required)
Each memory file starts with frontmatter:
---
name: <short title>
description: <one-line description>
type: user|feedback|reference|project
scope: personal|team
---
- user: user preferences and long-term facts
- feedback: user corrections and feedback
- reference: reference material and external knowledge
- project: project-specific knowledge and decisions

### memory vs plan vs tasks
- memory: knowledge worth keeping LONG-TERM → write to memory files
- plan: the current task plan → do NOT write to long-term memory
- tasks: short-term todo lists → do NOT write to long-term memory

### Current memory index
{index_content}
"""

_TEAMMEM_EXTRA = """
### Team memory (TEAMMEM mode)
You have TWO memory directories:
- Personal: `{memory_root}` — your own memories
- Team: `{team_root}` — shared by all team agents
Rules:
- Write generally useful, project-wide knowledge to the team directory with `scope: team`.
- Write personal or agent-specific knowledge to the personal directory with `scope: personal`.
- Tag every memory with the correct `scope` in its frontmatter.

### Team memory index
{team_index_content}
"""


def load_memory_prompt(store=None) -> str:
    """
    构建系统提示词中的记忆教学文本。

    标准模式：身份与位置声明 + 行为规则 + 四种记忆类型 + 动态索引内容
    TEAMMEM 模式：额外追加团队目录读写指引与 scope 标签规则
    """
    from app.core.memory_system.store import memory_store as default_store

    store = store or default_store
    if not store.enabled:
        return ""

    mode = store.mode
    memory_root = str(store.memory_root("personal"))
    index_content = store.load_index_content("personal") or "(empty)"

    prompt = _STANDARD_MEMORY_TEACHING.format(
        memory_root=memory_root,
        index_filename="MEMORY.md",
        index_content=index_content,
    )

    if mode == "teammem":
        team_root = str(store.memory_root("team"))
        team_index_content = store.load_index_content("team") or "(empty)"
        prompt += _TEAMMEM_EXTRA.format(
            memory_root=memory_root,
            team_root=team_root,
            team_index_content=team_index_content,
        )

    return prompt
