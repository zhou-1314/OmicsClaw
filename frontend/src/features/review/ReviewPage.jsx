import React, { useEffect, useState } from 'react';
import { getGroups, getGroupDiff, rollbackGroup, approveGroup, clearAll } from '../../lib/api';
import DiffViewer from '../../components/DiffViewer';
import { Activity, Check, FileText, ShieldCheck, Database, Box, Link, BookOpen, GitMerge, ChevronDown, ChevronRight, Sparkles, GitCommit } from 'lucide-react';
import clsx from 'clsx';

const ICONS = {
  nodes: Box, 
  memories: FileText, 
  edges: Link, 
  paths: Database, 
  glossary_keywords: BookOpen
};

const STYLES = {
  created: {
    bg: "bg-emerald-950/40 border-emerald-500/30 text-emerald-400",
    text: "text-emerald-400"
  },
  deleted: {
    bg: "bg-rose-950/40 border-rose-500/30 text-rose-400",
    text: "text-rose-400"
  },
  modified: {
    bg: "bg-amber-950/40 border-amber-500/30 text-amber-400",
    text: "text-amber-400"
  }
};

function ImpactRadiusGraph({ data }) {
  if (!data?.path_changes?.length) return null;

  return (
    <div className="mb-6 p-4 bg-slate-900/40 border border-slate-800/60 rounded-lg backdrop-blur-sm relative overflow-hidden">
      <GitMerge size={64} className="absolute -top-2 -right-2 opacity-5" />
      <h3 className="text-xs font-bold text-slate-500 uppercase mb-4 flex items-center gap-2 tracking-widest relative z-10">
        <GitMerge size={12} /> Impact Radius
      </h3>
      <div className="space-y-3 relative z-10">
        {data.path_changes.map((pc, i) => (
          <div key={i} className="flex items-center gap-3 text-sm">
            <span className={clsx("px-2 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider w-16 text-center border",
              pc.action === 'deleted' ? "text-rose-500/80 bg-rose-500/10 border-rose-500/20" : "text-emerald-500/80 bg-emerald-500/10 border-emerald-500/20"
            )}>
              {pc.action === 'deleted' ? 'Unlink' : 'Link'}
            </span>
            <div className="flex items-center gap-2 flex-1 min-w-0">
              <span className="w-1.5 h-1.5 rounded-full bg-slate-600" />
              <span className="w-8 h-[1px] bg-slate-700" />
              <span className={clsx("font-mono text-xs truncate px-2 py-1 rounded-md border",
                pc.action === 'deleted' ? "bg-rose-950/20 border-rose-900/50 text-rose-400/70 line-through" : "bg-emerald-950/20 border-emerald-900/50 text-emerald-400"
              )}>
                {pc.uri}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AISummary({ data, action }) {
  if (!data) return null;
  const sum = [];
  if (action === 'created') sum.push('Initialized node and its core metadata.');
  if (action === 'deleted') sum.push('Removed entity and purged associated paths.');
  if (action === 'modified') {
    if (data.has_changes) sum.push('Updated node content/metadata.');
    if (data.path_changes?.length) sum.push(`Modified ${data.path_changes.length} structural connections.`);
    if (data.glossary_changes?.length) sum.push(`Indexed ${data.glossary_changes.length} glossary terms.`);
  }
  if (!sum.length) sum.push('Performed internal alignment with no semantic shifts.');

  return (
    <div className="bg-gradient-to-r from-indigo-950/40 to-slate-900/20 border border-indigo-500/20 rounded-md p-4 mb-6 flex gap-3 text-sm animate-in fade-in duration-500">
      <Sparkles className="text-indigo-400 mt-0.5 flex-shrink-0" size={16} />
      <div className="text-indigo-100/90 leading-relaxed font-light">
        <span className="font-semibold block mb-1 text-xs uppercase tracking-widest text-indigo-400/80">Action Summary</span>
        <span>{sum.join(' ')}</span>
      </div>
    </div>
  );
}

function FeedItem({ item, onApprove, onReject }) {
  const [expanded, setExpanded] = useState(false);
  const [diff, setDiff] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const toggle = async () => {
    if (!expanded && !diff) {
      setLoading(true);
      try {
        setDiff(await getGroupDiff(item.node_uuid));
      } catch (err) {
        setError(err.message || 'Data unavailable');
      } finally {
        setLoading(false);
      }
    }
    setExpanded(!expanded);
  };

  const Icon = ICONS[item.top_level_table] || FileText;
  const style = STYLES[item.action] || STYLES.modified;

  return (
    <div className="relative pl-8 pb-8 group/item">
      <div className="absolute top-0 bottom-0 left-[15px] w-[2px] bg-slate-800/50 group-last/item:bg-gradient-to-b group-last/item:from-slate-800/50 group-last/item:to-transparent" />
      
      <div className={clsx("absolute top-4 left-0 w-[32px] h-[32px] rounded-full flex items-center justify-center border-2 border-slate-950 shadow-lg z-10 transition-colors duration-300",
        expanded ? style.bg : "bg-slate-800 border-slate-700 text-slate-500"
      )}>
        <GitCommit size={14} />
      </div>

      <div className={clsx("glass-panel rounded-xl border transition-all duration-300 overflow-hidden",
        expanded ? "border-indigo-500/30 shadow-[0_0_30px_rgba(99,102,241,0.1)]" : "border-slate-800/60 hover:bg-white/[0.02] hover:border-slate-700"
      )}>
        <div className="p-4 cursor-pointer flex items-center justify-between" onClick={toggle}>
          <div className="flex items-center gap-4 min-w-0">
            <button className="text-slate-500 hover:text-slate-300">{expanded ? <ChevronDown size={18}/> : <ChevronRight size={18}/>}</button>
            <div className="flex flex-col min-w-0">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
                <span className={clsx("font-bold capitalize", style.text)}>[{item.action}]</span>
                <span className="truncate">{item.display_uri}</span>
              </div>
              <div className="flex items-center gap-2 mt-1">
                <span className="flex items-center gap-1.5 text-xs text-slate-500 bg-slate-900/50 px-2 py-0.5 rounded-md border border-slate-800">
                  <Icon size={12} /> <span className="capitalize">{item.top_level_table}</span>
                </span>
                <span className="text-xs text-slate-600 tracking-wide uppercase">{item.row_count} ops</span>
              </div>
            </div>
          </div>
          
          <div className="flex items-center gap-2 opacity-0 group-hover/item:opacity-100 transition-opacity">
            <button onClick={(e) => { e.stopPropagation(); onReject(item); }} className="px-3 py-1.5 rounded bg-slate-800 hover:bg-rose-950/50 text-slate-400 hover:text-rose-400 border border-slate-700 text-xs font-medium transition-colors">Reject</button>
            <button onClick={(e) => { e.stopPropagation(); onApprove(item); }} className="px-3 py-1.5 rounded bg-indigo-600/10 hover:bg-indigo-500/20 text-indigo-400 hover:text-indigo-300 border border-indigo-500/30 text-xs font-bold transition-colors">Approve</button>
          </div>
        </div>

        {expanded && (
          <div className="p-6 border-t border-slate-800/60 bg-slate-900/20">
            {loading ? <div className="text-sm text-slate-500 animate-pulse flex items-center gap-3"><div className="w-4 h-4 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin"/> Analyzing impact profile...</div> : 
             error ? <div className="text-rose-400 text-sm">Failed to load details: {error}</div> : 
             diff ? (
              <div className="animate-in fade-in slide-in-from-top-2">
                <AISummary data={diff} action={item.action} />
                <ImpactRadiusGraph data={diff} />
                {(diff.current_content || diff.before_content) && (
                  <div className="rounded-lg overflow-hidden border border-slate-800/80 bg-[#1e1e1e] shadow-xl">
                    <div className="bg-slate-900 px-4 py-2 text-xs text-slate-500 border-b border-slate-800 font-mono">Payload Snapshot (JSON Diff)</div>
                    <div className="p-4 max-h-[400px] overflow-y-auto custom-scrollbar">
                      <DiffViewer oldText={diff.before_content ?? ''} newText={diff.current_content ?? ''} />
                    </div>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ReviewPage() {
  const [changes, setChanges] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => { loadChanges(); }, []);

  const loadChanges = async () => {
    setLoading(true);
    try { setChanges(await getGroups()); }
    catch { setError("Disconnected from Neural Core."); }
    finally { setLoading(false); }
  };

  const handleReject = async (item) => {
    if (!confirm(`Reject alterations to ${item.display_uri}?`)) return;
    try { await rollbackGroup(item.node_uuid); loadChanges(); }
    catch (err) { alert("Rejection failed: " + err.message); }
  };

  const handleApprove = async (item) => {
    try { await approveGroup(item.node_uuid); loadChanges(); }
    catch (err) { alert("Integration failed: " + err.message); }
  };

  const handleClearAll = async () => {
    if (!confirm("Integrate ALL pending memories into the active brain?")) return;
    try { await clearAll(); setChanges([]); }
    catch (err) { alert("Integration failed: " + err.message); }
  };

  return (
    <div className="flex h-full bg-slate-950 text-slate-300 overflow-y-auto custom-scrollbar relative">
      <div className="absolute top-0 left-0 right-0 h-96 bg-gradient-to-b from-indigo-900/10 via-purple-900/5 to-transparent pointer-events-none" />
      <div className="max-w-4xl w-full mx-auto p-8 relative z-10">
        <header className="mb-10 flex items-end justify-between border-b border-slate-800/50 pb-6">
          <div>
            <div className="flex items-center gap-3 mb-2">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-purple-900/20">
                <ShieldCheck className="w-5 h-5 text-white" />
              </div>
              <h1 className="text-2xl font-bold tracking-tight text-white">Review & Audit Protocol</h1>
            </div>
            <p className="text-sm text-slate-400 font-light mt-2 max-w-xl">
              Inspect semantic memory alterations proposed by agents. Feed items are clustered by action, featuring impact radius visualization and AI-generated insights.
            </p>
          </div>
          {changes.length > 0 && (
            <button onClick={handleClearAll} className="flex items-center gap-2 px-6 py-2.5 bg-emerald-600/10 hover:bg-emerald-500/20 text-emerald-400 hover:text-emerald-300 border border-emerald-500/30 rounded-lg text-sm font-bold shadow-[0_0_15px_rgba(16,185,129,0.1)] transition-all">
              <Check size={16} /> Approve All Pending
            </button>
          )}
        </header>

        {loading ? (
          <div className="flex justify-center p-20 opacity-50">
            <div className="w-8 h-8 flex items-center justify-center relative">
              <div className="absolute inset-0 border-2 border-indigo-500/20 rounded-full animate-ping"></div>
              <div className="w-4 h-4 rounded-full bg-indigo-500 animate-pulse"></div>
            </div>
          </div>
        ) : error ? (
          <div className="p-8 border border-rose-900/50 bg-rose-950/20 rounded-xl text-center">
            <Activity className="mx-auto text-rose-500 mb-4" size={32} />
            <p className="text-rose-400">{error}</p>
          </div>
        ) : changes.length > 0 ? (
          <div className="pt-4 pb-20">
            {changes.map(c => <FeedItem key={c.node_uuid} item={c} onApprove={handleApprove} onReject={handleReject} />)}
            <div className="relative pl-8 pt-4">
               <div className="absolute top-0 bottom-full left-[15px] w-[2px] bg-gradient-to-t from-transparent to-slate-800/50" />
               <div className="flex flex-col gap-1 items-start">
                 <div className="flex items-center gap-4 text-slate-500 text-sm font-medium">
                   <div className="w-2 h-2 rounded-full bg-slate-700 ml-[-5px]" />
                   End of pending payload
                 </div>
               </div>
            </div>
          </div>
        ) : (
          <div className="text-center py-20 animate-in fade-in duration-700">
            <div className="w-20 h-20 rounded-full bg-slate-900 border border-slate-800 flex items-center justify-center mx-auto mb-6 shadow-xl relative group">
               <div className="absolute inset-0 bg-emerald-500/10 blur-xl opacity-0 group-hover:opacity-100 transition-opacity rounded-full" />
               <Check size={32} className="text-slate-600 group-hover:text-emerald-500/50 transition-colors" />
            </div>
            <h3 className="text-lg font-medium text-slate-300">No Pending Memory Alterations</h3>
            <p className="text-slate-500 text-sm mt-2">When agents propose modifications to the neural graph, they will appear here awaiting your judgment.</p>
          </div>
        )}
      </div>
    </div>
  );
}
