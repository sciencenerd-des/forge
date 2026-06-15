# Database Schema Design

This schema is designed to support a stateful, multi-model agent loop where progress is persisted at every turn.

## Tables

### 1. goals
| Column | Type | Description |
| :--- | :--- | :--- |
| id | UUID (PK) | Unique identifier |
| title | TEXT | Human-readable title |
| description | TEXT | Detailed goal description |
| status | TEXT | 'active', 'completed', 'blocked' |
| success_criteria | JSONB | Array of strings or objects |
| priority | INT | 1 (Highest) to 5 (Lowest) |

### 2. tasks
| Column | Type | Description |
| :--- | :--- | :--- |
| id | UUID (PK) | Unique identifier |
| goal_id | UUID (FK) | Reference to goals.id |
| title | TEXT | Human-readable task name |
| description | TEXT | Detailed instructions for this task |
| status | TEXT | 'proposed', 'active', 'completed', 'blocked' |
| next_step | TEXT | The explicit next action for the Executor |
| priority | INT | 1 (Highest) to 5 (Lowest) |
| created_at | TIMESTAMP | |

### 3. memories
| Column | Type | Description |
| :--- | :--- | :--- |
| id | UUID (PK) | Unique identifier |
| content | TEXT | The actual memory content |
| type | TEXT | 'fact', 'decision', 'constraint', 'summary' |
| embedding | VECTOR | For semantic recall (pgvector) |
| metadata | JSONB | Contextual metadata (e.g., task_id, agent_run_id) |

### 4. artifacts
| Column | Type | Description |
| :--- | :--- | :--- |
| id | UUID (PK) | Unique identifier |
| task_id | UUID (FK) | Reference to tasks.id |
| path | TEXT | Path to the file/data |
| content_summary | TEXT | Brief summary of the artifact |
| type | TEXT | 'file', 'api_response', 'image', 'code_block' |

### 5. blockers
| Column | Type | Description |
| :--- | :--- | :--- |
| id | UUID (PK) | Unique identifier |
| task_id | UUID (FK) | Reference to tasks.id |
| blocker_type | TEXT | 'missing_info', 'tool_failure', 'model_hallucination', 'permission_denied' |
| description | TEXT | Detailed explanation of why the task is blocked |
| status | TEXT | 'open', 'resolved' |

### 6. agent_runs
| Column | Type | Description |
| :--- | :--- | :--- |
| id | UUID (PK) | Unique identifier |
| goal_id | UUID (FK) | Reference to goals.id |
| task_id | UUID (FK) | Reference to tasks.id |
| turn_count | INT | Current iteration number |
| summary | TEXT | Summary of what happened in this turn |
| next_action | TEXT | The planned next action |
| resume_instruction | TEXT | Explicit instructions for the next turn |
| timestamp | TIMESTAMP | |
