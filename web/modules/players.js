// Shared player index store — Alpine.store('players')
import { apiGet, normalizeName } from './api.js';

export default {
  loaded: false,
  byId: new Map(),
  byNormName: new Map(),
  options: [],

  async load() {
    if (this.loaded) return;
    try {
      const data = await apiGet('/api/players');
      if (!data || data.success !== true || !Array.isArray(data.players)) return;

      this.byId.clear();
      this.byNormName.clear();
      this.options = [];

      data.players.forEach((p) => {
        const id = Number(p.id ?? p.playerId);
        const name = String(p.name || '').trim();
        if (!Number.isFinite(id) || id <= 0 || !name) return;

        const entry = { id, name };
        this.byId.set(id, entry);

        const norm = normalizeName(name);
        const arr = this.byNormName.get(norm) || [];
        arr.push(entry);
        this.byNormName.set(norm, arr);

        this.options.push({ label: `${name} (${id})`, id, name });
      });

      this.loaded = true;
    } catch {
      // Keep UI functional
    }
  },

  resolve(nameInput) {
    const raw = String(nameInput || '').trim();
    if (!raw) return { id: null };

    const idMatch = raw.match(/\((\d+)\)\s*$/);
    if (idMatch) {
      const id = Number(idMatch[1]);
      if (Number.isFinite(id) && id > 0) return { id };
    }

    const cleaned = raw.replace(/\(\d+\)\s*$/, '').trim();
    const norm = normalizeName(cleaned);
    if (!norm) return { id: null };

    const exact = this.byNormName.get(norm) || [];
    if (exact.length === 1) return { id: exact[0].id };
    if (exact.length > 1) return { id: null, ambiguous: true, candidates: exact.slice(0, 6) };

    const prefix = [];
    for (const [key, values] of this.byNormName.entries()) {
      if (key.startsWith(norm)) prefix.push(...values);
    }
    if (prefix.length === 1) return { id: prefix[0].id };
    if (prefix.length > 1) return { id: null, ambiguous: true, candidates: prefix.slice(0, 6) };

    return { id: null };
  },

  nameForId(id) {
    const entry = this.byId.get(Number(id));
    return entry ? `${entry.name} (${entry.id})` : '';
  },
};
