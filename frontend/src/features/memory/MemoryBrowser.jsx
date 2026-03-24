import React, { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { 
  Folder, 
  Edit3, 
  Save, 
  X, 
  Cpu, 
  Hash, 
  AlertTriangle,
  Link2,
  Star,
  Database
} from 'lucide-react';
import clsx from 'clsx';
import { api } from '../../lib/api';
import PriorityBadge from './components/PriorityBadge';
import GlossaryHighlighter from './components/GlossaryHighlighter';
import KeywordManager from './components/KeywordManager';
import DomainNode from './components/MemorySidebar';
import Breadcrumb from './components/Breadcrumb';
import NodeGridCard from './components/NodeGridCard';

export default function MemoryBrowser() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domain = searchParams.get('domain') || 'core';
  const path = searchParams.get('path') || '';
  
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [data, setData] = useState({ node: null, children: [], breadcrumbs: [] });
  const [domains, setDomains] = useState([]);
  
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [editDisclosure, setEditDisclosure] = useState('');
  const [editPriority, setEditPriority] = useState(0);
  const [saving, setSaving] = useState(false);

  const currentRouteRef = useRef({ domain, path });
  useEffect(() => {
    currentRouteRef.current = { domain, path };
  }, [domain, path]);

  useEffect(() => {
    api.get('/browse/domains').then(res => setDomains(res.data)).catch(() => {});
  }, []);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      setEditing(false);
      try {
        const res = await api.get('/browse/node', { params: { domain, path } });
        setData(res.data);
        setEditContent(res.data.node?.content || '');
        setEditDisclosure(res.data.node?.disclosure || '');
        setEditPriority(res.data.node?.priority ?? 0);
      } catch (err) {
        setError(err.response?.data?.detail || err.message);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, [domain, path]);

  const navigateTo = (newPath, newDomain) => {
    const params = new URLSearchParams();
    params.set('domain', newDomain || domain);
    if (newPath) params.set('path', newPath);
    setSearchParams(params);
  };

  const refreshData = () => {
    return api.get('/browse/node', { params: { domain, path } })
      .then(res => {
        setData(currentData => {
          if (currentRouteRef.current.domain === domain && currentRouteRef.current.path === path) {
            return res.data;
          }
          return currentData;
        });
      });
  };

  const startEditing = () => {
    setEditContent(data.node?.content || '');
    setEditDisclosure(data.node?.disclosure || '');
    setEditPriority(data.node?.priority ?? 0);
    setEditing(true);
  };

  const cancelEditing = () => {
    setEditing(false);
    setEditContent(data.node?.content || '');
    setEditDisclosure(data.node?.disclosure || '');
    setEditPriority(data.node?.priority ?? 0);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload = {};
      if (editContent !== (data.node?.content || '')) payload.content = editContent;
      if (editPriority !== (data.node?.priority ?? 0)) payload.priority = editPriority;
      if (editDisclosure !== (data.node?.disclosure || '')) payload.disclosure = editDisclosure;
      
      if (Object.keys(payload).length === 0) {
        setEditing(false);
        return;
      }
      
      await api.put('/browse/node', payload, { params: { domain, path } });
      await refreshData();
      setEditing(false);
    } catch (err) {
      alert('Save failed: ' + err.message);
    } finally {
      setSaving(false);
    }
  };

  const isRoot = !path;
  const node = data.node;

  return (
    <div className="flex h-full bg-transparent text-slate-300 font-sans selection:bg-indigo-500/30 selection:text-indigo-200 overflow-hidden">
      
      {/* Sidebar */}
      <div className="w-64 flex-shrink-0 glass-panel border-r border-white/5 flex flex-col relative z-20 shadow-[10px_0_30px_rgba(0,0,0,0.5)]">
        <div className="p-5 border-b border-slate-800/30">
          <div className="flex items-center gap-2 text-indigo-400 mb-1">
            <Database size={18} />
            <h1 className="font-bold tracking-tight text-sm text-slate-100">Knowledge Graph</h1>
          </div>
          <p className="text-[10px] text-slate-600 pl-6 uppercase tracking-wider">Omics Navigator v2.0</p>
        </div>
        
        <div className="p-3 flex-1 overflow-y-auto custom-scrollbar">
             <div className="mb-4">
                 <h3 className="px-3 text-[10px] font-bold text-slate-600 uppercase tracking-widest mb-2">Domains</h3>
                 {domains.map(d => (
                   <DomainNode
                     key={d.domain}
                     domain={d.domain}
                     rootCount={d.root_count}
                     activeDomain={domain}
                     activePath={path}
                     onNavigate={navigateTo}
                   />
                 ))}
                 {domains.length === 0 && (
                   <DomainNode
                     domain="core"
                     activeDomain={domain}
                     activePath={path}
                     onNavigate={navigateTo}
                   />
                 )}
             </div>
        </div>

        <div className="mt-auto p-4 border-t border-slate-800/30">
             <div className="bg-slate-900/50 rounded p-3 border border-slate-800/50">
                 <div className="flex items-center gap-2 text-xs text-slate-500 mb-2">
                    <Hash size={12} />
                    <span>Current Path</span>
                 </div>
                 <code className="block text-[10px] font-mono text-indigo-300/80 break-all leading-tight">
                    {domain}://{path || 'root'}
                 </code>
             </div>
        </div>
      </div>

      {/* Main Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-transparent relative z-10">
         <div className="h-14 flex-shrink-0 border-b border-white/5 flex items-center px-6 bg-slate-900/30 backdrop-blur-xl sticky top-0 z-20 shadow-lg">
             <Breadcrumb items={data.breadcrumbs} onNavigate={navigateTo} />
         </div>

         <div className="flex-1 overflow-y-auto p-6 custom-scrollbar">
            {loading ? (
                <div className="h-full flex flex-col items-center justify-center gap-4 text-slate-600">
                    <div className="w-8 h-8 border-2 border-indigo-500/20 border-t-indigo-500 rounded-full animate-spin" />
                    <span className="text-xs tracking-widest uppercase">Retrieving Neural Data...</span>
                </div>
            ) : error ? (
                <div className="h-full flex flex-col items-center justify-center text-rose-500 gap-4">
                    <p className="text-lg">Access Denied / Error</p>
                    <p className="text-sm opacity-60">{error}</p>
                    <button onClick={() => navigateTo('')} className="text-xs bg-slate-800 px-4 py-2 rounded hover:text-white transition-colors">Return to Root</button>
                </div>
            ) : (
                <div className="max-w-7xl mx-auto space-y-8 animate-in fade-in slide-in-from-bottom-4 duration-500">
                    
                    {node && (!isRoot || !node.is_virtual || editing) && (
                        <div className="space-y-4">
                            <div className="flex items-start justify-between gap-4">
                                <div className="space-y-3 min-w-0 flex-1">
                                    <div className="flex items-center gap-3 flex-wrap">
                                        <h1 className="text-2xl font-bold text-slate-100 tracking-tight">
                                            {node.name || path.split('/').pop()}
                                        </h1>
                                        <PriorityBadge priority={node.priority} size="lg" />
                                    </div>
                                    
                                    {node.disclosure && !editing && (
                                        <div className="inline-flex items-center gap-2 px-3 py-1.5 bg-amber-950/20 border border-amber-900/30 rounded-lg text-amber-500/80 text-xs max-w-full">
                                            <AlertTriangle size={14} className="flex-shrink-0" />
                                            <span className="font-medium mr-1">Disclosure:</span>
                                            <span className="italic truncate">{node.disclosure}</span>
                                        </div>
                                    )}
                                    
                                    {node.aliases && node.aliases.length > 0 && !editing && (
                                        <div className="flex items-start gap-2 text-xs text-slate-500">
                                            <Link2 size={13} className="flex-shrink-0 mt-0.5 text-slate-600" />
                                            <div className="flex flex-wrap gap-1.5">
                                                <span className="text-slate-600 font-medium">Also reachable via:</span>
                                                {node.aliases.map(alias => (
                                                    <code key={alias} className="px-1.5 py-0.5 bg-slate-800/60 rounded text-indigo-400/70 font-mono text-[11px]">
                                                        {alias}
                                                    </code>
                                                ))}
                                            </div>
                                        </div>
                                    )}

                                    {!editing && !node.is_virtual && (
                                        <KeywordManager
                                          keywords={node.glossary_keywords || []}
                                          nodeUuid={node.node_uuid}
                                          onUpdate={refreshData}
                                        />
                                    )}
                                </div>
                                
                                <div className="flex gap-2 flex-shrink-0">
                                    {editing ? (
                                        <>
                                            <button onClick={cancelEditing} className="p-2 hover:bg-slate-800 rounded text-slate-400 transition-colors"><X size={18} /></button>
                                            <button onClick={handleSave} disabled={saving} className="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded text-sm font-medium transition-colors shadow-lg shadow-indigo-900/20">
                                                <Save size={16} /> {saving ? 'Saving...' : 'Save Changes'}
                                            </button>
                                        </>
                                    ) : (
                                        <button onClick={startEditing} className="flex items-center gap-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-sm font-medium transition-colors border border-slate-700 hover:border-slate-600">
                                            <Edit3 size={16} /> Edit
                                        </button>
                                    )}
                                </div>
                            </div>

                            {editing && (
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-4 bg-slate-900/50 border border-slate-800/50 rounded-xl">
                                    <div className="space-y-1.5">
                                        <label className="flex items-center gap-1.5 text-xs font-medium text-slate-400">
                                            <Star size={12} />
                                            Priority
                                            <span className="text-slate-600 font-normal">(lower = higher priority)</span>
                                        </label>
                                        <input 
                                            type="number"
                                            min="0"
                                            value={editPriority}
                                            onChange={e => setEditPriority(parseInt(e.target.value) || 0)}
                                            className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 font-mono focus:outline-none focus:border-indigo-500/50 transition-colors"
                                        />
                                    </div>
                                    <div className="space-y-1.5">
                                        <label className="flex items-center gap-1.5 text-xs font-medium text-slate-400">
                                            <AlertTriangle size={12} />
                                            Disclosure
                                            <span className="text-slate-600 font-normal">(when to recall)</span>
                                        </label>
                                        <input 
                                            type="text"
                                            value={editDisclosure}
                                            onChange={e => setEditDisclosure(e.target.value)}
                                            placeholder="e.g. When I need to remember..."
                                            className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-indigo-500/50 transition-colors"
                                        />
                                    </div>
                                </div>
                            )}

                            <div className={clsx(
                                "relative rounded-2xl border overflow-hidden transition-all duration-300 shadow-2xl backdrop-blur-lg",
                                editing ? "bg-slate-900/80 border-indigo-500/50 shadow-[0_0_30px_rgba(99,102,241,0.15)]" : "glass-panel"
                            )}>
                                {editing ? (
                                    <textarea 
                                        value={editContent}
                                        onChange={e => setEditContent(e.target.value)}
                                        className="w-full h-96 p-6 bg-transparent text-slate-200 font-mono text-sm leading-relaxed focus:outline-none resize-y"
                                        spellCheck={false}
                                    />
                                ) : (
                                    <div className="p-6 md:p-8">
                                        {(() => {
                                          let contentStr = node.content || '';
                                          let parsed = null;

                                          if (contentStr.trim().startsWith('{') && contentStr.trim().endsWith('}')) {
                                            try { parsed = JSON.parse(contentStr); } catch (e) {}
                                          }
                                          
                                          if (parsed && typeof parsed === 'object') {
                                            return (
                                              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                                {Object.entries(parsed).map(([k, v]) => (
                                                  <div key={k} className="bg-slate-900/40 p-3 rounded-lg border border-slate-800/50 shadow-inner">
                                                    <span className="text-[10px] uppercase font-bold text-indigo-400/80 mb-1.5 block tracking-widest">{k.replace(/_/g, ' ')}</span>
                                                    <div className="text-sm text-slate-300 font-mono break-words whitespace-pre-wrap">
                                                       {typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}
                                                    </div>
                                                  </div>
                                                ))}
                                              </div>
                                            );
                                          }
                                          return (
                                            <div className="prose prose-invert prose-sm max-w-none">
                                              <GlossaryHighlighter
                                                key={node.node_uuid}
                                                content={contentStr}
                                                glossary={node.glossary_matches || []}
                                                currentNodeUuid={node.node_uuid}
                                                onNavigate={navigateTo}
                                              />
                                            </div>
                                          );
                                        })()}
                                    </div>
                                )}
                            </div>
                        </div>
                    )}

                    {data.children && data.children.length > 0 && (
                        <div className="space-y-4 pt-4">
                            <div className="flex items-center gap-3 text-slate-500">
                                <h2 className="text-xs font-bold uppercase tracking-widest">
                                    {isRoot ? "Memory Clusters" : "Sub-Nodes"}
                                </h2>
                                <div className="h-px flex-1 bg-slate-800/50"></div>
                                <span className="text-xs bg-slate-800/50 px-2 py-0.5 rounded-full">{data.children.length}</span>
                            </div>
                            
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                                {data.children.map(child => (
                                    <NodeGridCard 
                                        key={`${child.domain || domain}:${child.path}`} 
                                        node={child}
                                        currentDomain={domain}
                                        onClick={() => navigateTo(child.path, child.domain)} 
                                    />
                                ))}
                            </div>
                        </div>
                    )}
                    
                    {!loading && !data.children?.length && !node && (
                        <div className="flex flex-col items-center justify-center py-20 text-slate-600 gap-4">
                            <Folder size={48} className="opacity-20" />
                            <p className="text-sm">Empty Sector</p>
                        </div>
                    )}
                </div>
            )}
         </div>
      </div>
    </div>
  );
}
