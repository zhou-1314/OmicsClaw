import React, { useState, useEffect, useCallback } from 'react';
import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom';
import { ShieldCheck, Database, LayoutGrid, Sparkles } from 'lucide-react';
import clsx from 'clsx';

import ReviewPage from './features/review/ReviewPage';
import MemoryBrowser from './features/memory/MemoryBrowser';
import MaintenancePage from './features/maintenance/MaintenancePage';
import TokenAuth from './components/TokenAuth';
import { AUTH_ERROR_EVENT } from './lib/api';

function Layout() {
  return (
    <div className="flex flex-col h-screen bg-transparent text-slate-200">
      {/* Top Navigation Bar */}
      <div className="h-14 border-b border-white/5 bg-slate-900/40 backdrop-blur-xl flex items-center px-6 gap-6 flex-shrink-0 z-10 shadow-lg">
        <div className="font-bold tracking-tight flex items-center gap-2 mr-4">
          <LayoutGrid className="w-5 h-5 text-indigo-400 drop-shadow-md" />
          <span className="text-gradient font-bold">OmicsClaw Memory</span>
        </div>

        <nav className="flex items-center gap-1 h-full">
          <NavLink
            to="/review"
            className={({ isActive }) => clsx(
              "h-full flex items-center gap-2 px-4 text-sm font-medium border-b-2 transition-colors",
              isActive ? "border-indigo-500 text-indigo-400 bg-slate-800/50" : "border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/30"
            )}
          >
            <ShieldCheck size={16} />
            Review & Audit
          </NavLink>

          <NavLink
            to="/memory"
            className={({ isActive }) => clsx(
              "h-full flex items-center gap-2 px-4 text-sm font-medium border-b-2 transition-colors",
              isActive ? "border-emerald-500 text-emerald-400 bg-slate-800/50" : "border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/30"
            )}
          >
            <Database size={16} />
            Memory Explorer
          </NavLink>

          <NavLink
            to="/maintenance"
            className={({ isActive }) => clsx(
              "h-full flex items-center gap-2 px-4 text-sm font-medium border-b-2 transition-colors",
              isActive ? "border-amber-500 text-amber-400 bg-slate-800/50" : "border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/30"
            )}
          >
            <Sparkles size={16} />
            Brain Cleanup
          </NavLink>
        </nav>
      </div>

      {/* Main Area */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <Routes>
          <Route path="/" element={<Navigate to="/review" replace />} />

          <Route path="/review" element={<ReviewPage />} />

          <Route path="/memory" element={<MemoryBrowser />} />

          <Route path="/maintenance" element={<MaintenancePage />} />
        </Routes>
      </div>
    </div>
  );
}

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    return !!localStorage.getItem('api_token');
  });
  const [isCheckingAuth, setIsCheckingAuth] = useState(true);

  const handleAuthError = useCallback(() => {
    setIsAuthenticated(false);
  }, []);

  const handleAuthenticated = useCallback(() => {
    setIsAuthenticated(true);
  }, []);

  // 组件挂载时，如果当前未认证，尝试发送一个无 token 的请求探测后端是否开启了鉴权
  useEffect(() => {
    let mounted = true;

    const checkAuthStatus = async () => {
      if (isAuthenticated) {
        if (mounted) setIsCheckingAuth(false);
        return;
      }

      try {
        const { getDomains } = await import('./lib/api');
        await getDomains();
        if (mounted) {
          setIsAuthenticated(true);
          setIsCheckingAuth(false);
        }
      } catch (error) {
        if (mounted) {
          setIsCheckingAuth(false);
        }
      }
    };

    checkAuthStatus();

    return () => {
      mounted = false;
    };
  }, [isAuthenticated]);

  // 监听 401 事件，切换回认证界面
  useEffect(() => {
    window.addEventListener(AUTH_ERROR_EVENT, handleAuthError);
    return () => {
      window.removeEventListener(AUTH_ERROR_EVENT, handleAuthError);
    };
  }, [handleAuthError]);

  if (isCheckingAuth) {
    return (
      <div className="flex flex-col items-center justify-center h-screen bg-slate-950 text-slate-400">
        <div className="w-8 h-8 rounded-full border-2 border-indigo-500/30 border-t-indigo-500 animate-spin mb-4"></div>
        <div className="text-sm">Connecting to Memory Core...</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <TokenAuth onAuthenticated={handleAuthenticated} />;
  }

  return (
    <BrowserRouter>
      <Layout />
    </BrowserRouter>
  );
}

export default App;
