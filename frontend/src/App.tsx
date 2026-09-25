import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';

const API = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
const SOURCE_URL = import.meta.env.VITE_SOURCE_URL;
const examples = [
  { label: 'Export users table to CSV', prompt: 'Extract users table from the DB', type: 'ETL', icon: '◇' },
  { label: 'Average fare by ride status', prompt: 'What is the average fare by ride status?', type: 'SQL', icon: '▦' },
  { label: 'Export filtered PayPal payments', prompt: 'Filter payments by payment method of PAYPAL and extract to CSV', type: 'ETL', icon: '◇' },
  { label: 'Filter ratings to a CSV', prompt: 'Filter the demo ratings to ratings of at least 4.5 and export CSV.', type: 'ETL', icon: '◇' },
  { label: 'Extract a public JSON URL', prompt: 'Extract https://jsonplaceholder.typicode.com/todos, filter completed equals true, and save to CSV.', type: 'ETL', icon: '◇' },
];

type Result = {
  request_id: string;
  status: 'completed' | 'blocked' | 'unsupported' | 'failed';
  route: 'sql' | 'etl' | null;
  answer: string;
  table: { columns: string[]; rows: (string | number | boolean | null)[][]; truncated: boolean } | null;
  artifact: { id: string; source: string; name: string; format: string; size_bytes: number; download_url: string } | null;
  details: { sql: string | null; sql_safe: boolean | null; operation: string | null };
  error: { code: string; message: string } | null;
};

type Entry = { id: string; prompt: string; result?: Result; error?: string };

function ResultCard({ result, onTryTransform, onSelectSource }: { result: Result; onTryTransform: (prompt: string) => void; onSelectSource: (id: string) => void }) {
  return <div className="answer-card">
    <div className="answer-top"><span className={'route-tag ' + (result.route || 'notice')}>{result.route === 'sql' ? 'SQL analyst' : result.route === 'etl' ? 'ETL analyst' : 'Demo guardrail'}</span><span className={'result-status ' + result.status}>{result.status}</span></div>
    <p className="answer-text">{result.answer}</p>
    {result.error && <p className="hint">{result.error.message}</p>}
    {result.status !== 'completed' && <p className="request-ref">Request ID: <code>{result.request_id}</code></p>}
    {result.table && <div className="table-wrap" role="region" aria-label="Result table" tabIndex={0}><table><thead><tr>{result.table.columns.map((column, i) => <th key={i} scope="col">{column.replaceAll('_', ' ')}</th>)}</tr></thead><tbody>{result.table.rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell == null ? '—' : String(cell)}</td>)}</tr>)}</tbody></table>{!result.table.rows.length && <div className="empty-table">No matching rows.</div>}{result.table.truncated && <div className="table-foot">Showing a preview of the results</div>}</div>}
    {result.artifact && <a className="download" href={API + result.artifact.download_url} download><span className="file-icon">↓</span><span><strong>{result.artifact.name}</strong><small>{result.artifact.format.toUpperCase()} · {(result.artifact.size_bytes / 1024).toFixed(1)} KB · temporary download</small></span><span className="download-arrow">↗</span></a>}
    {result.artifact && <div className="transform-actions"><button type="button" onClick={() => onSelectSource(result.artifact!.id)}>Use as transformation source</button>{result.artifact.source === 'demo database public.users' && <><button type="button" onClick={() => { onSelectSource(result.artifact!.id); onTryTransform('Filter active users from exported users CSV'); }}>Filter active users</button><button type="button" onClick={() => { onSelectSource(result.artifact!.id); onTryTransform('Count users by province from exported users CSV'); }}>Count by province</button></>}</div>}
    {(result.details.sql || result.details.operation) && <details className="details"><summary>How it worked <span>⌄</span></summary><div className="details-content">{result.details.sql && <><div className="detail-label">Validated read-only SQL</div><pre>{result.details.sql}</pre></>}{result.details.operation && <><div className="detail-label">ETL operation</div><code>{result.details.operation}</code><p>Output was written to a temporary flat file. ETL does not write to the database.</p></>}</div></details>}
  </div>;
}

export default function App() {
  const [prompt, setPrompt] = useState('');
  const [entries, setEntries] = useState<Entry[]>([]);
  const [pending, setPending] = useState(false);
  const [selectedSourceId, setSelectedSourceId] = useState('');
  const artifacts = entries.flatMap(entry => entry.result?.artifact ? [entry.result.artifact] : []);
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking');
  const [sessionSeconds, setSessionSeconds] = useState<number | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    fetch(API + '/api/session', { credentials: 'include' })
      .then(async response => {
        if (!response.ok) throw new Error('SESSION_EXPIRED');
        return response.json() as Promise<{ expires_in: number }>;
      })
      .then(data => { setSessionSeconds(data.expires_in); setSessionExpired(false); })
      .catch(() => { setSessionSeconds(0); setSessionExpired(true); });
    fetch(API + '/api/health').then(r => setHealth(r.ok ? 'online' : 'offline')).catch(() => setHealth('offline'));
  }, []);
  useEffect(() => {
    if (sessionSeconds === null || sessionExpired) return;
    const timer = window.setInterval(() => {
      setSessionSeconds(previous => {
        if (previous === null || previous <= 1) {
          setSessionExpired(true);
          setEntries([]);
          setSelectedSourceId('');
          return 0;
        }
        return previous - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [sessionSeconds === null, sessionExpired]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [entries, pending]);

  function expireSession() {
    setSessionSeconds(0);
    setSessionExpired(true);
    setEntries([]);
    setSelectedSourceId('');
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const message = prompt.trim();
    if (!message || pending || sessionExpired) return;
    const sourceArtifactId = selectedSourceId || undefined;
    const id = crypto.randomUUID();
    setEntries(prev => [...prev, { id, prompt: message }]);
    setPrompt(''); setPending(true);
    try {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), 60000);
      let response: Response;
      try { response = await fetch(API + '/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ message, source_artifact_id: sourceArtifactId, source_mode: sourceArtifactId ? 'artifact' : 'auto' }), signal: controller.signal }); }
      finally { clearTimeout(timer); }
      const data = await response.json();
      if (response.status === 410 && data.detail?.code === 'SESSION_EXPIRED') { expireSession(); throw new Error('This session has expired. Reload the page to start a new session.'); }
      if (!response.ok) throw new Error(data.detail?.message || (response.status === 422 ? 'Please enter a shorter, valid question.' : `Request failed (${response.status}).`));
      setEntries(prev => prev.map(item => item.id === id ? { ...item, result: data as Result } : item));
      setHealth('online');
    } catch (error) {
      const text = error instanceof DOMException && error.name === 'AbortError' ? 'This request took too long. The demo may be waking up; please retry.' : error instanceof Error ? error.message : 'The demo is unavailable. Please retry.';
      setEntries(prev => prev.map(item => item.id === id ? { ...item, error: text } : item));
    } finally { setPending(false); }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit(); }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">DA<span>·</span></div><div><strong>Data Agent</strong><small>Agentic data workspace</small></div></div>
      <div className="side-label">WORKSPACE</div>
      <div className="side-link active"><span>◫</span> Playground</div>
      <div className="side-divider" />
      <div className="side-label">THE WORKFLOW</div>
      <div className="flow-item"><span className="flow-dot blue">1</span><div><strong>Route</strong><small>Classify the request</small></div></div>
      <div className="flow-item"><span className="flow-dot purple">2</span><div><strong>Analyze</strong><small>SQL or flat-file ETL</small></div></div>
      <div className="flow-item"><span className="flow-dot green">3</span><div><strong>Deliver</strong><small>Answer or download</small></div></div>
      <div className="sidebar-bottom"><div className="demo-pill"><span className="live-dot" /> Portfolio demo</div><p>Read-only database access. ETL exports flat files only.</p>{SOURCE_URL && <a href={SOURCE_URL} target="_blank" rel="noreferrer">Source repository ↗</a>}</div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> Playground</div><div className="top-actions"><div className={'health ' + health}><span />{health === 'online' ? 'Service online' : health === 'checking' ? 'Checking service' : 'Service waking up'}</div>{sessionSeconds !== null && <div className={'session-clock' + (sessionExpired ? ' expired' : '')}>{sessionExpired ? 'Session expired' : `Session ${Math.floor(sessionSeconds / 60)}:${String(sessionSeconds % 60).padStart(2, '0')}`}</div>}<button className="reset" onClick={() => { setEntries([]); setSelectedSourceId(''); }} disabled={pending || entries.length === 0}>New session</button></div></header>
      <div className="content">
        <div className="page-intro"><div className="eyebrow">YOUR DATA, IN PLAIN ENGLISH <span>✦</span></div><h1>Ask the data.<br /><em>Get clear answers.</em></h1><p>Explore demo data with a routed SQL analyst and a flat-file ETL agent. Every answer shows what happened behind the scenes.</p></div>
        {entries.length === 0 && <section className="examples"><div className="section-heading"><h2>Try a starting point</h2><span>EXAMPLE PROMPTS</span></div><div className="example-grid">{examples.map(example => <button key={example.label} className="example" onClick={() => setPrompt(example.prompt)}><span className="example-icon">{example.icon}</span><span className="example-name">{example.label}</span><span className="example-type">{example.type}</span><span className="example-arrow">↗</span></button>)}</div></section>}
        {entries.length > 0 && <section className="conversation" aria-live="polite">{entries.map(entry => <div className="exchange" key={entry.id}><div className="user-line"><span className="avatar">YOU</span><p>{entry.prompt}</p></div>{entry.result && <ResultCard result={entry.result} onTryTransform={setPrompt} onSelectSource={setSelectedSourceId} />}{entry.error && <div className="error-card"><strong>Could not complete this request</strong><p>{entry.error}</p></div>}{!entry.result && !entry.error && <div className="thinking"><span className="spinner" /> Routing your request and preparing the result…</div>}</div>)}<div ref={endRef} /></section>}
        <div className="composer-wrap"><form className="composer" onSubmit={submit}>{artifacts.length > 0 && <label className="source-selector">Transform a file <select aria-label="Transformation source" value={selectedSourceId} onChange={event => setSelectedSourceId(event.target.value)} disabled={pending || sessionExpired}><option value="">No file selected — use database or a new source</option>{artifacts.map((artifact, index) => <option key={artifact.id} value={artifact.id}>{index + 1}. {artifact.name}</option>)}</select><small>The selected file is used for follow-up filters. Say “from the database” or clear the selection to query the database.</small></label>}<textarea aria-label="Ask the data agent" value={prompt} onChange={e => setPrompt(e.target.value)} onKeyDown={onKeyDown} placeholder={sessionExpired ? 'Session expired — reload the page to continue' : 'Ask a question or describe a flat-file operation…'} maxLength={1000} rows={2} disabled={pending || sessionExpired} /><div className="composer-bottom"><span>Enter to send <span className="key-hint">·</span> Shift + Enter for a new line</span><button type="submit" disabled={pending || sessionExpired || !prompt.trim()}>{sessionExpired ? 'Session expired' : pending ? 'Working…' : 'Run request'} <span>↗</span></button></div></form><p className="disclaimer">Each request runs independently on synthetic demo data. Temporary downloads expire with this 15-minute session.</p></div>
      </div>
    </main>
  </div>;
}
