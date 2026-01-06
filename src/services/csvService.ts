import { Product } from '../types';

const REQUIRED_COLUMNS = ['code', 'name', 'price', 'currency'];

function parseLine(line: string): string[] {
  // Parse simple CSV without quotes nesting for este MVP
  const parts: string[] = [];
  let current = '';
  let inQuotes = false;

  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"') {
      inQuotes = !inQuotes;
      continue;
    }
    if (char === ',' && !inQuotes) {
      parts.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  parts.push(current.trim());
  return parts;
}

export async function parseCsv(file: File): Promise<Product[]> {
  const text = await file.text();
  if (!text.trim()) {
    throw new Error('El archivo está vacío');
  }

  const lines = text.replace(/\r/g, '').split('\n').filter((l) => l.trim().length > 0);
  const header = parseLine(lines.shift() as string);

  for (const column of REQUIRED_COLUMNS) {
    if (!header.includes(column)) {
      throw new Error(`Falta la columna obligatoria: ${column}`);
    }
  }

  const columnIndex: Record<string, number> = {};
  header.forEach((col, idx) => {
    columnIndex[col] = idx;
  });

  const products: Product[] = lines.map((line, idx) => {
    const parts = parseLine(line);
    const code = parts[columnIndex['code']] || '';
    const name = parts[columnIndex['name']] || '';
    const priceRaw = parts[columnIndex['price']] || '0';
    const currency = (parts[columnIndex['currency']] || 'ARS').toUpperCase();
    const category = parts[columnIndex['category']] || 'General';

    const price = Number(priceRaw);
    if (Number.isNaN(price)) {
      throw new Error(`Precio inválido en la fila ${idx + 2}`);
    }

    return {
      id: `${code}-${idx}`,
      code,
      name,
      price,
      currency: currency === 'USD' ? 'USD' : 'ARS',
      category
    } as Product;
  });

  return products;
}
