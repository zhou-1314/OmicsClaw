import axios from 'axios';

export const AUTH_ERROR_EVENT = 'omicsclaw:auth-error';
const KG_WORKSPACE_KEY = 'omicsclaw_kg_workspace';

const getApiToken = () => localStorage.getItem('api_token');

export const normalizeKgWorkspace = (value) => {
  const trimmed = String(value || '').trim();
  if (!trimmed) return '';
  const normalized = trimmed.replace(/[\\/]+$/, '');
  if (normalized.endsWith('/.omicsclaw/knowledge') || normalized.endsWith('\\.omicsclaw\\knowledge')) {
    return normalized;
  }
  return `${normalized}/.omicsclaw/knowledge`;
};

export const getKgWorkspace = () => normalizeKgWorkspace(localStorage.getItem(KG_WORKSPACE_KEY));

export const setKgWorkspace = (value) => {
  const normalized = normalizeKgWorkspace(value);
  if (normalized) localStorage.setItem(KG_WORKSPACE_KEY, normalized);
  else localStorage.removeItem(KG_WORKSPACE_KEY);
  return normalized;
};

const attachCommonInterceptors = (client, { withKgWorkspace = false } = {}) => {
  client.interceptors.request.use((config) => {
    const token = getApiToken();
    if (token) {
      config.headers = config.headers ?? {};
      config.headers.Authorization = `Bearer ${token}`;
    }
    if (withKgWorkspace) {
      const workspace = getKgWorkspace();
      if (workspace) {
        config.headers = config.headers ?? {};
        config.headers['X-OmicsClaw-Workspace'] = workspace;
      }
    }
    return config;
  });

  client.interceptors.response.use(
    (response) => response,
    (error) => {
      if (error.response && error.response.status === 401) {
        localStorage.removeItem('api_token');
        window.dispatchEvent(new CustomEvent(AUTH_ERROR_EVENT));
      }
      return Promise.reject(error);
    }
  );
};

export const api = axios.create({
  baseURL: '/api'
});

export const kgApi = axios.create({
  baseURL: '/kg'
});

attachCommonInterceptors(api);
attachCommonInterceptors(kgApi, { withKgWorkspace: true });

const encodeId = (id) => encodeURIComponent(id);

// ============ Review API ============

export const getChanges = () =>
  api.get('/review/changes').then(res => res.data);

export const getChangeCount = () =>
  api.get('/review/change-count').then(res => res.data);

export const getDiff = (key) =>
  api.get(`/review/diff?key=${encodeId(key)}`).then(res => res.data);

export const rollbackChanges = (keys) =>
  api.post('/review/rollback', { keys }).then(res => res.data);

export const integrateChanges = (keys) =>
  api.post('/review/integrate', { keys }).then(res => res.data);

export const integrateAll = () =>
  api.post('/review/integrate-all').then(res => res.data);

// ============ Group-based Review API (used by ReviewPage) ============

/**
 * Get all pending changes grouped by node_uuid, transformed into the shape
 * that ReviewPage / SnapshotList expect.
 */
export const getGroups = async () => {
  const { groups } = await getChanges();
  return Object.entries(groups).map(([nodeUuid, entries]) => {
    // Determine the primary table & action from the first entry
    const firstEntry = entries[0] || {};
    const table = firstEntry.table || 'unknown';
    const before = firstEntry.before;
    const after = firstEntry.after;
    const ref = after || before;

    let action = 'modified';
    if (!before && after) action = 'created';
    else if (before && !after) action = 'deleted';

    // Build a display URI from the ref data
    let displayUri = nodeUuid;
    if (ref) {
      displayUri = ref.uri || ref.path || ref.keyword || ref.uuid || nodeUuid;
    }

    return {
      node_uuid: nodeUuid,
      display_uri: displayUri,
      top_level_table: table,
      action,
      row_count: entries.length,
      _keys: entries.map(e => e.key).filter(Boolean),
      _entries: entries,
    };
  });
};

/**
 * Get diff information for a specific node group.
 * Fetches diffs for all change keys in the group and merges them.
 */
export const getGroupDiff = async (nodeUuid) => {
  // First get the full changes to find the keys belonging to this group
  const { groups } = await getChanges();
  const entries = groups[nodeUuid] || [];

  if (entries.length === 0) {
    throw new Error(`No changes found for node ${nodeUuid}`);
  }

  // Collect all diffs for each key in the group
  let beforeContent = '';
  let currentContent = '';
  let beforeMeta = {};
  let currentMeta = {};
  let action = 'modified';
  let hasChanges = false;
  const pathChanges = [];
  const glossaryChanges = [];
  const activePaths = [];

  for (const entry of entries) {
    const table = entry.table || 'unknown';
    const before = entry.before;
    const after = entry.after;

    if (!before && after) action = 'created';
    else if (before && !after) action = 'deleted';

    if (table === 'memories') {
      // Fetch detailed diff via the diff endpoint if we have a key
      if (entry.key) {
        try {
          const diff = await getDiff(entry.key);
          beforeContent = diff.before?.content || '';
          currentContent = diff.after?.content || '';
          hasChanges = beforeContent !== currentContent;
        } catch {
          // Fall back to inline data
          beforeContent = before?.content || '';
          currentContent = after?.content || '';
          hasChanges = beforeContent !== currentContent;
        }
      } else {
        beforeContent = before?.content || '';
        currentContent = after?.content || '';
        hasChanges = beforeContent !== currentContent;
      }
    } else if (table === 'nodes') {
      beforeMeta = {
        priority: before?.priority ?? null,
        disclosure: before?.disclosure ?? null,
      };
      currentMeta = {
        priority: after?.priority ?? null,
        disclosure: after?.disclosure ?? null,
      };
      if (JSON.stringify(beforeMeta) !== JSON.stringify(currentMeta)) {
        hasChanges = true;
      }
    } else if (table === 'paths') {
      pathChanges.push({
        uri: (after || before)?.path || '',
        action: !before ? 'created' : !after ? 'deleted' : 'modified',
      });
      if (after?.path) activePaths.push(after.path);
      hasChanges = true;
    } else if (table === 'glossary_keywords') {
      glossaryChanges.push({
        keyword: (after || before)?.keyword || '',
        action: !before ? 'created' : !after ? 'deleted' : 'modified',
      });
      hasChanges = true;
    } else if (table === 'edges') {
      hasChanges = true;
    }
  }

  return {
    action,
    has_changes: hasChanges,
    before_content: beforeContent,
    current_content: currentContent,
    before_meta: beforeMeta,
    current_meta: currentMeta,
    path_changes: pathChanges,
    glossary_changes: glossaryChanges,
    active_paths: activePaths,
  };
};

/**
 * Rollback all changes belonging to a specific node group.
 */
export const rollbackGroup = async (nodeUuid) => {
  const { groups } = await getChanges();
  const entries = groups[nodeUuid] || [];
  const keys = entries.map(e => e.key).filter(Boolean);

  if (keys.length === 0) {
    throw new Error('No rollback keys found for this group');
  }

  const result = await rollbackChanges(keys);
  if (result.errors?.length) {
    const error = new Error(
      `Rollback partially failed: ${result.errors.map(e => `${e.key}: ${e.error}`).join('; ')}`
    );
    error.reviewResult = result;
    throw error;
  }
  return result;
};

/**
 * Approve (integrate) all changes belonging to a specific node group.
 */
export const approveGroup = async (nodeUuid) => {
  const { groups } = await getChanges();
  const entries = groups[nodeUuid] || [];
  const keys = entries.map(e => e.key).filter(Boolean);

  if (keys.length === 0) {
    throw new Error('No approval keys found for this group');
  }

  const result = await integrateChanges(keys);
  if (result.errors?.length) {
    const error = new Error(
      `Approve partially failed: ${result.errors.map(e => `${e.key}: ${e.error}`).join('; ')}`
    );
    error.reviewResult = result;
    throw error;
  }
  return result;
};

/**
 * Clear/integrate all pending changes.
 */
export const clearAll = () => integrateAll();

// ============ Browse API ============

export const getDomains = () =>
  api.get('/browse/domains').then(res => res.data);

export const getNode = (path, domain = 'core') =>
  api.get(`/browse/node?path=${encodeId(path)}&domain=${domain}`).then(res => res.data);

export const getChildren = (nodeUuid, domain, path) => {
  const params = new URLSearchParams({ node_uuid: nodeUuid });
  if (domain) params.append('domain', domain);
  if (path) params.append('path', path);
  return api.get(`/browse/children?${params}`).then(res => res.data);
};

export const getAllPaths = (domain) => {
  const params = domain ? `?domain=${domain}` : '';
  return api.get(`/browse/paths${params}`).then(res => res.data);
};

export const searchMemories = (query, limit = 10, domain) => {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  if (domain) params.append('domain', domain);
  return api.get(`/browse/search?${params}`).then(res => res.data);
};

export const getRecent = (limit = 10) =>
  api.get(`/browse/recent?limit=${limit}`).then(res => res.data);

export const createMemory = (data) =>
  api.post('/browse/create', data).then(res => res.data);

export const updateNode = (path, domain, data) =>
  api.put(`/browse/node?path=${encodeId(path)}&domain=${domain}`, data).then(res => res.data);

export const addPath = (data) =>
  api.post('/browse/add-path', data).then(res => res.data);

export const removePath = (path, domain = 'core') =>
  api.delete(`/browse/path?path=${encodeId(path)}&domain=${domain}`).then(res => res.data);

// ============ Glossary API ============

export const getAllGlossary = () =>
  api.get('/browse/glossary').then(res => res.data);

export const addGlossary = (keyword, nodeUuid) =>
  api.post('/browse/glossary', { keyword, node_uuid: nodeUuid }).then(res => res.data);

export const removeGlossary = (keyword, nodeUuid) =>
  api.delete('/browse/glossary', { data: { keyword, node_uuid: nodeUuid } }).then(res => res.data);

// ============ Maintenance API ============

export const getOrphans = () =>
  api.get('/maintenance/orphans').then(res => res.data);

export const getOrphanDetail = (memoryId) =>
  api.get(`/maintenance/orphan/${memoryId}`).then(res => res.data);

export const deleteOrphan = (memoryId) =>
  api.delete(`/maintenance/orphan/${memoryId}`).then(res => res.data);

export const rebuildSearchIndex = () =>
  api.post('/maintenance/rebuild-search-index').then(res => res.data);

// ============ Health API ============

export const getHealth = () =>
  api.get('/health').then(res => res.data);

// ============ KG API ============

export const getKgStatus = () =>
  kgApi.get('/status').then(res => res.data);

export const getKgHealth = () =>
  kgApi.get('/health').then(res => res.data);

export const searchKg = (query, { pageType, status, state, limit = 20 } = {}) => {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  if (pageType) params.append('type', pageType);
  if (status) params.append('status', status);
  if (state) params.append('state', state);
  return kgApi.get(`/search?${params}`).then(res => res.data);
};

export const listKgPages = (pageType, { status, state, limit = 50 } = {}) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.append('status', status);
  if (state) params.append('state', state);
  return kgApi.get(`/pages/${encodeURIComponent(pageType)}?${params}`).then(res => res.data);
};

export const getKgPage = (pageType, slug, { includeNotes = false } = {}) => {
  const params = includeNotes ? '?include_notes=true' : '';
  return kgApi.get(`/pages/${encodeURIComponent(pageType)}/${encodeURIComponent(slug)}${params}`)
    .then(res => res.data);
};

export default api;
