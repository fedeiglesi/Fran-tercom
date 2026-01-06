import { Product } from '../types';

const STOP_WORDS = new Set([
  'para', 'de', 'el', 'la', 'con', 'los', 'las', 'y', 'un', 'una', 'moto', 'motos', 'en', 'del'
]);

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

function scoreProduct(query: string, product: Product): number {
  const normQuery = normalize(query);
  const tokens = tokenize(query);
  const code = product._normCode || normalize(product.code);
  const name = product._normName || normalize(product.name);

  let score = 0;

  if (code === normQuery) {
    score += 50000;
  }

  if (code.includes(normQuery) && normQuery.length > 0) {
    score += 15000;
  }

  for (const token of tokens) {
    if (name.includes(token)) {
      score += 2000;
    }
    if (code.startsWith(token)) {
      score += 500;
    }
  }

  return score;
}

export function indexProducts(products: Product[]): Product[] {
  return products.map((p) => ({
    ...p,
    _normName: normalize(p.name),
    _normCode: normalize(p.code)
  }));
}

export function searchProducts(query: string, products: Product[], limit = 8): Product[] {
  const scored = products
    .map((product) => ({ product, score: scoreProduct(query, product) }))
    .filter((item) => item.score >= 1500)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);

  return scored.map((s) => s.product);
}
