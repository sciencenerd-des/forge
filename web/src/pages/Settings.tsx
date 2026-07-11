import { useEffect, useState } from 'react'
import { Check, KeyRound, LoaderCircle, Save, Server, WifiOff } from 'lucide-react'
import { ProviderClient, type ProviderProfile, type ProviderTestResult } from '../lib/providers'

const ROLES = ['default', 'planner', 'executor', 'auditor', 'evaluator', 'steward']
const PRESETS = {
  'LM Studio': { base_url: 'http://localhost:1234/v1', auth_mode: 'api_key' },
  Ollama: { base_url: 'http://localhost:11434/v1', auth_mode: 'none' },
  vLLM: { base_url: 'http://localhost:8000/v1', auth_mode: 'api_key' },
}

const providerClient = new ProviderClient()

export default function Settings() {
  const [profiles, setProfiles] = useState<Record<string, ProviderProfile>>({})
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { providerClient.list().then((document) => setProfiles(document.profiles)).catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load providers')) }, [])

  return <section className="grid gap-4 p-4 md:p-6">
    <div><div className="section-label">Provider setup</div><h2 className="mt-1 text-2xl font-black uppercase text-white">Connect the models Forge should use.</h2><p className="mt-2 max-w-2xl text-sm text-slate-500">Profiles are stored locally. Saved keys are never returned; the masked value only confirms that one exists.</p></div>
    {error ? <div role="alert" className="panel text-sm text-red-300">{error}</div> : null}
    <div className="grid gap-4 xl:grid-cols-2">{ROLES.map((role) => <ProviderCard key={role} role={role} initial={profiles[role]} onSaved={(profile) => setProfiles((current) => ({ ...current, [role]: profile }))} />)}</div>
  </section>
}

function ProviderCard({ role, initial, onSaved }: { role: string; initial?: ProviderProfile; onSaved: (profile: ProviderProfile) => void }) {
  const [baseUrl, setBaseUrl] = useState(initial?.base_url ?? PRESETS['LM Studio'].base_url)
  const [model, setModel] = useState(initial?.model ?? 'auto')
  const [apiKey, setApiKey] = useState('')
  const [authMode, setAuthMode] = useState(initial?.auth_mode ?? 'api_key')
  const [result, setResult] = useState<ProviderTestResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)

  const test = async () => { setBusy(true); setResult(null); try { setResult(await providerClient.test(role, { base_url: baseUrl, model, api_key: apiKey || undefined, auth_mode: authMode })) } catch (e) { setResult({ status: 'unreachable', message: e instanceof Error ? e.message : 'Test failed' }) } finally { setBusy(false) } }
  const save = async () => { setBusy(true); try { const profile = await providerClient.update(role, { base_url: baseUrl, model, auth_mode: authMode, ...(apiKey ? { api_key: apiKey } : {}) }); onSaved(profile); setApiKey(''); setSaved(true) } catch (e) { setResult({ status: 'unreachable', message: e instanceof Error ? e.message : 'Save failed' }) } finally { setBusy(false) } }

  return <div className="panel"><div className="flex items-center justify-between"><div className="flex items-center gap-2 text-base font-bold uppercase text-white"><Server size={16} className="text-amber-300" />{role}</div>{saved ? <span className="flex items-center gap-1 text-xs text-emerald-300"><Check size={13} />Saved</span> : null}</div>
    <div className="mt-4 flex flex-wrap gap-2">{Object.entries(PRESETS).map(([name, preset]) => <button key={name} className="control-button" onClick={() => { setBaseUrl(preset.base_url); setAuthMode(preset.auth_mode) }}>{name}</button>)}</div>
    <label className="mt-4 block text-xs uppercase tracking-wider text-slate-500">Base URL<input className="mt-1 w-full rounded border border-white/10 bg-black/20 p-2 font-mono text-sm text-slate-200" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></label>
    <label className="mt-3 block text-xs uppercase tracking-wider text-slate-500">Model<input className="mt-1 w-full rounded border border-white/10 bg-black/20 p-2 font-mono text-sm text-slate-200" value={model} onChange={(e) => setModel(e.target.value)} /></label>
    <label className="mt-3 block text-xs uppercase tracking-wider text-slate-500"><span className="flex items-center gap-1"><KeyRound size={13} />API key (optional; blank preserves saved key)</span><input type="password" autoComplete="new-password" className="mt-1 w-full rounded border border-white/10 bg-black/20 p-2 font-mono text-sm text-slate-200" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={initial?.api_key || 'not-needed'} /></label>
    {result ? <div className={`mt-3 flex items-center gap-2 text-sm ${result.status === 'reachable' ? 'text-emerald-300' : 'text-red-300'}`}>{result.status === 'reachable' ? <Check size={15} /> : <WifiOff size={15} />}{result.status}{result.models?.length ? ` · ${result.models.join(', ')}` : result.message ? ` · ${result.message}` : ''}</div> : null}
    <div className="mt-4 flex justify-end gap-2"><button className="control-button" disabled={busy} onClick={() => void test()}>{busy ? <LoaderCircle className="animate-spin" size={14} /> : <WifiOff size={14} />}Test</button><button className="control-button" disabled={busy} onClick={() => void save()}><Save size={14} />Save</button></div>
  </div>
}
