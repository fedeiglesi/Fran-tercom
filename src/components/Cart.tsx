import { Minus, Plus, ShoppingCart, Trash2 } from 'lucide-react';
import { CartItem, Product } from '../types';

interface CartProps {
  items: CartItem[];
  onUpdate: (items: CartItem[]) => void;
}

export default function Cart({ items, onUpdate }: CartProps) {
  const updateQty = (product: Product, delta: number) => {
    const next = items
      .map((item) =>
        item.id === product.id ? { ...item, quantity: Math.max(1, item.quantity + delta) } : item
      )
      .filter((item) => item.quantity > 0);
    onUpdate(next);
  };

  const removeItem = (id: string) => {
    onUpdate(items.filter((i) => i.id !== id));
  };

  const total = items.reduce((sum, item) => sum + item.price * item.quantity, 0);

  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-4 h-full flex flex-col">
      <div className="flex items-center gap-2 mb-3">
        <ShoppingCart className="w-5 h-5 text-indigo-500" />
        <h3 className="font-semibold text-slate-800">Carrito</h3>
      </div>
      <div className="space-y-2 overflow-y-auto flex-1">
        {items.map((item) => (
          <div key={item.id} className="border border-slate-200 rounded-xl p-3">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="text-sm text-slate-500">[{item.code}]</div>
                <div className="font-semibold text-slate-800">{item.name}</div>
                <div className="text-sm text-green-600">${item.price.toLocaleString('es-AR')} {item.currency}</div>
              </div>
              <button className="text-red-500" onClick={() => removeItem(item.id)}>
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
            <div className="flex items-center gap-2 mt-2">
              <button className="p-1 bg-slate-100 rounded" onClick={() => updateQty(item, -1)}>
                <Minus className="w-4 h-4" />
              </button>
              <span className="px-2 text-sm">{item.quantity}</span>
              <button className="p-1 bg-slate-100 rounded" onClick={() => updateQty(item, 1)}>
                <Plus className="w-4 h-4" />
              </button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="text-slate-500">Todavía no agregaste repuestos.</div>}
      </div>
      <div className="pt-3 border-t border-slate-100 mt-3">
        <div className="flex items-center justify-between">
          <span className="font-semibold text-slate-700">Total</span>
          <span className="text-green-600 font-bold">${total.toLocaleString('es-AR')} ARS</span>
        </div>
        <button className="mt-3 w-full bg-indigo-600 text-white py-2 rounded-xl hover:bg-indigo-700">
          Finalizar pedido
        </button>
      </div>
    </div>
  );
}
