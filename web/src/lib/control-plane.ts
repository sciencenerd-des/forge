export interface ControlPlaneRun {
  id: string
  project_id: string
  goal_id: string
  provider_id: string | null
  status: string
  current_node: string
  turn: number
  max_turns: number
  lease_owner: string | null
  lease_expires_at: string | null
  heartbeat_at: string | null
}

export interface Approval {
  id: string
  run_id: string
  action_type: string
  action_preview: Record<string, unknown>
  risk: 'low' | 'medium' | 'high' | 'critical'
  status: string
  requested_by: string
  expires_at: string
}

export interface RuntimeRun {
  id: string
  project_id: string
  project_name: string
  goal_title: string
  status: string
  current_node: string
  batch: number
  pid: number | null
  updated_at: string | null
  active_task: string | null
  attempt_count: number
  no_progress_count: number
  task_total: number
  task_completed: number
  file_count: number
  test_count: number
  log_tail: string[]
  model: string
}

export interface RuntimeSystem {
  control_plane: { status: string; pid: number }
  gateway: { status: string; pid: number | null }
  active_pge_runs: number
}

export interface RuntimeConfig {
  home: string
  workspaces: string
  database: string
  default_project: string
  provider: { base_url: string; model: string; kind: string }
  roles: Record<string, string>
}

export interface ProjectSummary {
  id: string
  name: string
  repo_path: string | null
  goal_count: number
  active_goal: string | null
  active_goal_status: string | null
  task_total: number
  task_completed: number
}

export class ControlPlaneClient {
  private readonly baseUrl: string
  private readonly token: string

  constructor(baseUrl = '/api', token = '') {
    this.baseUrl = baseUrl
    this.token = token
  }

  listRuns(): Promise<ControlPlaneRun[]> {
    return this.request('/runs')
  }

  listRuntimeRuns(): Promise<RuntimeRun[]> {
    return this.request('/runtime/runs')
  }

  getRuntimeSystem(): Promise<RuntimeSystem> {
    return this.request('/runtime/system')
  }

  getRuntimeConfig(): Promise<RuntimeConfig> {
    return this.request('/runtime/config')
  }

  listProjects(): Promise<ProjectSummary[]> {
    return this.request('/runtime/projects')
  }

  stopRun(projectId: string): Promise<{ status: string }> {
    return this.request(`/runtime/runs/${encodeURIComponent(projectId)}/stop`, { method: 'POST' })
  }

  startRun(goal: string): Promise<{ status: string; run_id: string }> {
    return this.request('/runtime/runs/start', {
      method: 'POST',
      body: JSON.stringify({ goal }),
    })
  }

  listApprovals(status?: string): Promise<Approval[]> {
    const query = status ? `?status=${encodeURIComponent(status)}` : ''
    return this.request(`/approvals${query}`)
  }

  decideApproval(id: string, approved: boolean, reason: string): Promise<Approval> {
    return this.request(`/approvals/${encodeURIComponent(id)}/decision`, {
      method: 'POST',
      body: JSON.stringify({ actor: 'web-operator', approved, reason }),
    })
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        'Content-Type': 'application/json',
        ...init.headers,
      },
    })
    if (!response.ok) {
      const detail = await response.text()
      throw new Error(`Control plane returned ${response.status}: ${detail.slice(0, 500)}`)
    }
    return response.json() as Promise<T>
  }
}
