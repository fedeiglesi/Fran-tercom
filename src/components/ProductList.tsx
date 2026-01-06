import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import { Product } from '../types';
import { searchProducts } from '../services/searchEngine';

interface ProductListProps {
  products: Product[];
  onAdd: (product: Product) => void;
}

export default function ProductList({ products, onAdd }: ProductListProps) {
  const [query, setQuery] = useState('');

  const results = useMemo(() => {
    if (!query.trim()) return products.slice(0, 8);
    return searchProducts(query, products, 8);
  }, [query, products]);

  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-4 h-full flex flex-col">
      <div className="flex items-center gap-2 mb-3">
        <Search className="w-5 h-5 text-indigo-500" />
        <input
          className="flex-1 border border-slate-200 rounded-xl px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-400"
          placeholder="Buscar por código, nombre o categoría"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="overflow-y-auto space-y-2 flex-1">
        {results.map((p) => (
          <div key={p.id} className="p-3 border border-slate-200 rounded-xl hover:border-indigo-200">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm text-slate-500">[{p.code}]</div>
                <div className="font-semibold text-slate-800">{p.name}</div>
                <div className="text-sm text-green-600">${p.price.toLocaleString('es-AR')} {p.currency}</div>
              </div>
              <button
                className="px-3 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700"
                onClick={() => onAdd(p)}
              >
                Agregar
              </button>
            </div>
          </div>
        ))}
        {results.length === 0 && <div className="text-slate-500">Sin resultados</div>}
      </div>
    </div>
  );
}
