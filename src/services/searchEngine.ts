import { Product } from '../types';

const STOP_WORDS = new Set([
  'para',
  'de',
  'el',
  'la',
  'con',
  'los',
  'las',
  'y',
  'un',
  'una',
  'moto',
  'motos',
  'en',
  'del'
]);

const SYNONYM_MAP: Record<string, string> = {
  ruleman: 'rodamiento',
  rulemanes: 'rodamientos',
  goma: 'cubierta',
  gomas: 'cubiertas',
  valvula: 'válvula',
  valvulas: 'válvulas',
  focos: 'lamparas',
  foco: 'lampara'
};

function normalize(text: string): string {
  return text
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function tokenize(text: string): string[] {
  return normalize(text)
    .split(' ')
    .filter((token) => token.length > 0 && !STOP_WORDS.has(token));
}

function expandWithSynonyms(tokens: string[]): string[] {
  const expanded = new Set<string>();
  for (const token of tokens) {
    expanded.add(token);
    if (SYNONYM_MAP[token]) {
      expanded.add(SYNONYM_MAP[token]);
    }
  }
  return Array.from(expanded);
}

function toNgrams(text: string, n = 3): string[] {
  const clean = normalize(text).replace(/\s+/g, '');
  const grams: string[] = [];
  for (let i = 0; i <= clean.length - n; i += 1) {
    grams.push(clean.slice(i, i + n));
  }
  return grams;
}

function scoreProduct(query: string, product: Product): number {
  const normQuery = normalize(query);
  const queryTokens = expandWithSynonyms(tokenize(query));
  const code = product._normCode || normalize(product.code);
  const name = product._normName || normalize(product.name);
  const tokens = product._tokens ?? expandWithSynonyms(tokenize(product.name));
  const ngrams = product._ngrams ?? toNgrams(product.name);

  let score = 0;

  // Código exacto
  if (code === normQuery && normQuery.length > 0) {
    score += 50000;
  }

  // Coincidencia de palabra completa + sinónimos
  const tokenHits: string[] = [];
  for (const token of queryTokens) {
    if (tokens.some((t) => t === token)) {
      score += 2000;
      tokenHits.push(token);
    } else if (tokens.some((t) => t === SYNONYM_MAP[token])) {
      score += 1500;
      tokenHits.push(token);
    }
  }

  // Bono de coincidencia total
  if (tokenHits.length === queryTokens.length && queryTokens.length > 0) {
    score += 5000;
  }

  // Coincidencia por prefijo de código (ya normalizado)
  for (const token of queryTokens) {
    if (code.startsWith(token)) {
      score += 500;
    }
  }

  // Bono de n-gramas (fuzzy)
  const queryNgrams = toNgrams(query);
  const overlap = queryNgrams.filter((g) => ngrams.includes(g));
  score += overlap.length * 500;

  // Palabras sueltas en nombre como fallback
  if (!tokenHits.length && name.includes(normQuery) && normQuery.length > 0) {
    score += 15000;
  }

  return score;
}

export function indexProducts(products: Product[]): Product[] {
  return products.map((p) => {
    const _normName = normalize(p.name);
    const _normCode = normalize(p.code);
    const _tokens = expandWithSynonyms(tokenize(p.name));
    const _ngrams = toNgrams(p.name);
    return {
      ...p,
      _normName,
      _normCode,
      _tokens,
      _ngrams
    };
  });
}

export function searchProducts(query: string, products: Product[], limit = 8): Product[] {
  const scored = products
    .map((product) => ({ product, score: scoreProduct(query, product) }))
    .filter((item) => item.score >= 1500)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);

  return scored.map((s) => s.product);
}
