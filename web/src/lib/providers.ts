export interface ProviderProfile {
  base_url?: string
  model?: string
  api_key: string
  auth_mode?: string
}

export interface ProviderDocument {
  version: 1
  profiles: Record<string, ProviderProfile>
}

export interface ProviderTestResult {
  status: 'reachable' | 'auth_error' | 'unreachable' | 'invalid'
  models?: string[]
  message?: string
}

export class ProviderClient {
  private readonly baseUrl: string

  constructor(baseUrl = '/api') { this.baseUrl = baseUrl }

  list(): Promise<ProviderDocument> { return this.request('/providers') }

  update(role: string, profile: Omit<ProviderProfile, 'api_key'> & { api_key?: string }): Promise<ProviderProfile> {
    return this.request(`/providers/${encodeURIComponent(role)}`, { method: 'PUT', body: JSON.stringify(profile) })
  }

  test(role: string, profile: Partial<ProviderProfile>): Promise<ProviderTestResult> {
    return this.request(`/providers/${encodeURIComponent(role)}/test`, { method: 'POST', body: JSON.stringify(profile) })
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers } })
    if (!response.ok) throw new Error(`Provider API returned ${response.status}: ${(await response.text()).slice(0, 300)}`)
    return response.json() as Promise<T>
  }
}
