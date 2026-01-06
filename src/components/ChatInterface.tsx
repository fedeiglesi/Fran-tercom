import { useState } from 'react';
import { Loader2, Send, Sparkles } from 'lucide-react';
import { ChatMessage, Product } from '../types';

interface ChatInterfaceProps {
  messages: ChatMessage[];
  hits: Product[];
  onSend: (message: string) => Promise<void>;
  isSending: boolean;
}

export default function ChatInterface({ messages, hits, onSend, isSending }: ChatInterfaceProps) {
  const [input, setInput] = useState('');

  const handleSend = async () => {
    if (!input.trim()) return;
    await onSend(input.trim());
    setInput('');
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto space-y-3 p-4 bg-white rounded-2xl border border-slate-200">
        {messages.length === 0 && (
          <div className="text-slate-500 flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-indigo-500" />
            Arrancá el chat con Fran.
          </div>
        )}
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`max-w-xl p-3 rounded-2xl shadow ${msg.role === 'user' ? 'bg-indigo-50 ml-auto' : 'bg-slate-50'}`}
          >
            <div className="text-xs text-slate-500 mb-1">
              {msg.role === 'user' ? 'Vos' : 'Fran'} • {msg.timestamp.toLocaleTimeString()}
            </div>
            <div className={msg.isError ? 'text-red-500' : 'text-slate-800'}>{msg.text}</div>
          </div>
        ))}
      </div>

      {hits.length > 0 && (
        <div className="mt-3 p-3 bg-indigo-50 border border-indigo-100 rounded-2xl max-h-48 overflow-y-auto">
          <h4 className="text-indigo-700 font-semibold mb-2">Repuestos encontrados</h4>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {hits.map((p) => (
              <div key={p.id} className="bg-white rounded-xl border border-slate-200 p-2">
                <div className="text-sm font-semibold text-slate-800">[{p.code}] {p.name}</div>
                <div className="text-sm text-green-600">
                  ${p.price.toLocaleString('es-AR')} {p.currency}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="mt-3 flex items-center gap-2">
        <input
          className="flex-1 rounded-xl border border-slate-200 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-400"
          placeholder="Preguntale a Fran..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          disabled={isSending}
        />
        <button
          className="bg-indigo-600 text-white px-4 py-2 rounded-xl flex items-center gap-2 hover:bg-indigo-700 disabled:opacity-60"
          onClick={handleSend}
          disabled={isSending}
        >
          {isSending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />} Enviar
        </button>
      </div>
    </div>
  );
}
