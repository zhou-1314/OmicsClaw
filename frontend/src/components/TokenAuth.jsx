import React, { useState, useCallback } from 'react';
import { LayoutGrid, KeyRound, Loader2, AlertCircle } from 'lucide-react';
import { getDomains } from '../lib/api';

const TokenAuth = ({ onAuthenticated }) => {
  const [token, setToken] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = useCallback(async (e) => {
    e.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) return;

    setLoading(true);
    setError('');

    // 先保存 token 到 localStorage，让 axios 拦截器能读取到
    localStorage.setItem('api_token', trimmed);

    try {
      await getDomains();
      onAuthenticated();
    } catch (err) {
      localStorage.removeItem('api_token');
      if (err.response && err.response.status === 401) {
        setError('Token 无效，请检查后重试');
      } else {
        setError('连接失败，请检查服务器状态');
      }
    } finally {
      setLoading(false);
    }
  }, [token, onAuthenticated]);

  return (
    <div className="flex items-center justify-center min-h-screen bg-transparent relative">
      <div className="absolute inset-0 bg-[url('https://www.transparenttextures.com/patterns/cubes.png')] opacity-[0.03] pointer-events-none"></div>
      <div className="w-full max-w-sm mx-4 relative z-10">
        {/* 卡片容器 */}
        <div className="glass-panel rounded-2xl p-8 shadow-[0_0_50px_rgba(79,70,229,0.15)] relative overflow-hidden">
          <div className="absolute -top-24 -right-24 w-48 h-48 bg-indigo-500/20 rounded-full blur-3xl mix-blend-screen pointer-events-none"></div>
          <div className="absolute -bottom-24 -left-24 w-48 h-48 bg-emerald-500/10 rounded-full blur-3xl mix-blend-screen pointer-events-none"></div>
          {/* Logo 区域 */}
          <div className="flex flex-col items-center mb-8 relative z-10">
            <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500/20 to-purple-500/10 border border-white/10 flex items-center justify-center mb-5 shadow-inner backdrop-blur-md">
              <LayoutGrid className="w-7 h-7 text-indigo-400 drop-shadow-md" />
            </div>
            <h1 className="text-xl font-bold tracking-tight text-white">OmicsClaw <span className="text-gradient">Memory</span></h1>
            <p className="text-xs text-slate-400 mt-2 font-medium">Please authenticate to continue</p>
          </div>

          {/* 表单 */}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label
                htmlFor="api-token"
                className="block text-xs font-medium text-slate-400 mb-2"
              >
                请输入 API Token
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <KeyRound className="w-4 h-4 text-slate-600" />
                </div>
                <input
                  id="api-token"
                  type="password"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value);
                    if (error) setError('');
                  }}
                  placeholder="Enter access token..."
                  disabled={loading}
                  className="w-full pl-10 pr-4 py-3 bg-black/20 border border-white/10 rounded-xl text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-400/70 focus:ring-2 focus:ring-indigo-500/20 backdrop-blur-sm transition-all focus:bg-black/40 disabled:opacity-50"
                />
              </div>
            </div>

            {/* 错误提示 */}
            {error && (
              <div className="flex items-center gap-2 text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded-lg px-3 py-2">
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !token.trim()}
              className="w-full py-3 bg-gradient-to-r from-indigo-500 to-violet-600 hover:from-indigo-400 hover:to-violet-500 disabled:from-slate-800 disabled:to-slate-800 disabled:text-slate-500 text-white text-sm font-semibold rounded-xl transition-all shadow-[0_4px_14px_0_rgba(99,102,241,0.39)] hover:shadow-[0_6px_20px_rgba(99,102,241,0.23)] hover:-translate-y-0.5 disabled:shadow-none disabled:transform-none flex items-center justify-center gap-2 relative z-10"
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Authenticating...
                </>
              ) : (
                'Connect to Brain'
              )}
            </button>
          </form>
        </div>

        {/* 底部文字 */}
        <p className="text-center text-[10px] text-slate-700 mt-4 tracking-wider uppercase">
          OmicsClaw Memory
        </p>
      </div>
    </div>
  );
};

export default TokenAuth;
