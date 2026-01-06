import express from 'express';
import bodyParser from 'body-parser';
import path from 'path';
import { fileURLToPath } from 'url';
import { GoogleGenAI } from '@google/genai';

const app = express();
app.use(bodyParser.json({ limit: '2mb' }));
app.use(bodyParser.urlencoded({ extended: true }));

let products = [];
const waHistories = new Map();
const processedSids = new Set();
setInterval(() => processedSids.clear(), 600000);

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const distPath = path.join(__dirname, 'dist');

const FRAN_SYSTEM_INSTRUCTION = `Eres Fran de Tercom, vendedor mayorista con 30 años en el mostrador.
TONO: Directo, muy argentino ("Che", "Mirá", "Va como piña"). Sabés mucho de motos.
REGLAS:
1. Siempre ARS. Conversión USD usando el Dólar Tercom informado en el contexto.
2. No inventes códigos ni precios. Si no está en el catálogo, decí que "lo buscás en el depósito".
3. Sé breve y servicial. Si te saludan, saludá con onda.`;

const STOP_WORDS = new Set(['para', 'de', 'el', 'la', 'con', 'los', 'las', 'y', 'un', 'una', 'moto', 'motos', 'en', 'del']);
const SYNONYM_MAP = {
  ruleman: 'rodamiento',
  rulemanes: 'rodamientos',
  goma: 'cubierta',
  gomas: 'cubiertas',
  valvula: 'valvula',
  valvulas: 'valvulas',
  focos: 'lamparas',
  foco: 'lampara'
};

function normalize(text = '') {
  return text
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function tokenize(text) {
  return normalize(text)
    .split(' ')
    .filter((token) => token.length > 0 && !STOP_WORDS.has(token));
}

function expandWithSynonyms(tokens = []) {
  const expanded = new Set();
  for (const token of tokens) {
    expanded.add(token);
    if (SYNONYM_MAP[token]) {
      expanded.add(SYNONYM_MAP[token]);
    }
  }
  return Array.from(expanded);
}

function toNgrams(text, n = 3) {
  const clean = normalize(text).replace(/\s+/g, '');
  const grams = [];
  for (let i = 0; i <= clean.length - n; i += 1) {
    grams.push(clean.slice(i, i + n));
  }
  return grams;
}

function enrichProduct(p) {
  return {
    ...p,
    _normName: normalize(p.name),
    _normCode: normalize(p.code),
    _tokens: expandWithSynonyms(tokenize(p.name)),
    _ngrams: toNgrams(p.name)
  };
}

function scoreProduct(query, product) {
  const normQuery = normalize(query);
  const queryTokens = expandWithSynonyms(tokenize(query));
  const code = product._normCode || normalize(product.code);
  const name = product._normName || normalize(product.name);
  const tokens = product._tokens || expandWithSynonyms(tokenize(product.name));
  const ngrams = product._ngrams || toNgrams(product.name);

  let score = 0;

  // Código exacto
  if (code === normQuery && normQuery.length > 0) {
    score += 50000;
  }

  const tokenHits = [];
  for (const token of queryTokens) {
    if (tokens.some((t) => t === token)) {
      score += 2000;
      tokenHits.push(token);
    } else if (tokens.some((t) => t === SYNONYM_MAP[token])) {
      score += 1500;
      tokenHits.push(token);
    }
    if (code.startsWith(token)) {
      score += 500;
    }
  }

  // Bono si todas las palabras aparecen
  if (tokenHits.length === queryTokens.length && queryTokens.length > 0) {
    score += 5000;
  }

  // Bono de n-gramas para fuzzy
  const queryNgrams = toNgrams(query);
  const overlap = queryNgrams.filter((g) => ngrams.includes(g));
  score += overlap.length * 500;

  // Fallback: coincidencia parcial de string completo
  if (!tokenHits.length && name.includes(normQuery) && normQuery.length > 0) {
    score += 15000;
  }

  return score;
}

function searchInCatalog(query, catalog, limit = 5) {
  return catalog
    .map((product) => ({ product, score: scoreProduct(query, product) }))
    .filter((item) => item.score >= 1500)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((item) => item.product);
}

function cleanHistory(history = []) {
  // Garantizar alternancia user/model para Gemini
  const cleaned = [];
  let lastRole = null;
  for (const turn of history) {
    if (turn.role !== lastRole) {
      cleaned.push(turn);
      lastRole = turn.role;
    }
  }
  return cleaned;
}

async function processFranQuery(message, history = [], exchangeRate = 1000) {
  if (!process.env.GEMINI_API_KEY) {
    return { text: 'Che, me falta la llave (GEMINI_API_KEY)...', hits: [] };
  }

  const hits = searchInCatalog(message, products);
  const context = hits.length > 0
    ? `CATÁLOGO DISPONIBLE (TC: $${exchangeRate}):\n` +
      hits
        .map((p) => {
          const price = p.currency === 'USD' ? p.price * exchangeRate : p.price;
          return `- [${p.code}] ${p.name} -> $${Math.round(price).toLocaleString('es-AR')} ARS`;
        })
        .join('\n')
    : products.length === 0
      ? 'AVISO: El catálogo está vacío.'
      : 'AVISO: No se encontraron repuestos. Respondé de forma general o saludá.';

  const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
  const validatedHistory = cleanHistory(history);

  try {
    const response = await ai.models.generateContent({
      model: 'gemini-3-flash-preview',
      contents: [
        ...validatedHistory,
        { role: 'user', parts: [{ text: `${context}\n\nMENSAJE: ${message}` }] }
      ],
      config: { systemInstruction: FRAN_SYSTEM_INSTRUCTION, temperature: 0.7 }
    });

    return { text: response.text || 'Che, me quedé sin palabras.', hits };
  } catch (error) {
    console.error('Error consultando Gemini', error);
    return { text: 'Bancame, se recalentó el motor.', hits };
  }
}

app.post('/api/catalog', (req, res) => {
  try {
    const { newProducts } = req.body || {};
    if (!Array.isArray(newProducts)) {
      return res.status(400).json({ success: false, message: 'Falta el catálogo para sincronizar' });
    }
    // Pre-indexado para acelerar búsquedas
    products = newProducts.map((p) => enrichProduct(p));
    return res.json({ success: true, count: products.length });
  } catch (error) {
    console.error('Error en /api/catalog', error);
    return res.status(500).json({ success: false, message: 'No se pudo actualizar el catálogo' });
  }
});

app.post('/api/chat', async (req, res) => {
  try {
    const { message, history = [], exchangeRate = 1000 } = req.body || {};
    if (!message) {
      return res.status(400).json({ text: 'Mensaje vacío', hits: [] });
    }

    const result = await processFranQuery(message, history, exchangeRate);
    return res.json(result);
  } catch (error) {
    console.error('Error en /api/chat', error);
    return res.status(500).json({ text: 'Fran está ocupado. Intentalo de nuevo.', hits: [] });
  }
});

app.post('/api/whatsapp', async (req, res) => {
  const sid = req.body?.MessageSid;
  const from = req.body?.From || 'default';
  const body = req.body?.Body || '';

  // Anti-loop: si ya procesamos este SID devolvemos XML vacío
  if (processedSids.has(sid)) {
    return res.status(200).send('<Response></Response>');
  }
  processedSids.add(sid);

  try {
    const rateRes = await fetch('https://dolarapi.com/v1/dolares/oficial')
      .then((r) => r.json())
      .catch(() => ({ venta: 1050 }));
    const rate = rateRes.venta || 1050;

    let history = waHistories.get(from) || [];
    const result = await processFranQuery(body, history, rate);

    history.push({ role: 'user', parts: [{ text: body }] }, { role: 'model', parts: [{ text: result.text }] });
    waHistories.set(from, history.slice(-6));

    res.set('Content-Type', 'text/xml');
    return res.send(`<Response><Message>${result.text}</Message></Response>`);
  } catch (error) {
    console.error('Error en /api/whatsapp', error);
    res.set('Content-Type', 'text/xml');
    return res.send('<Response><Message>Fran está ocupado. Escribime en un toque.</Message></Response>');
  }
});

app.use(express.static(distPath));
app.get('*', (req, res) => {
  res.sendFile(path.join(distPath, 'index.html'));
});

const PORT = process.env.PORT || 8080;
app.listen(PORT, () => {
  // Log claro para entorno Railway / local
  console.log(`Servidor Fran listo en puerto ${PORT}`);
});
