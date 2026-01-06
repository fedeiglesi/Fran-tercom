import { LogIn, Sparkles } from 'lucide-react';
import { AppState } from '../types';

interface LoginProps {
  onEnter: (next: AppState) => void;
}

export default function Login({ onEnter }: LoginProps) {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-indigo-500 to-slate-900 text-white">
      <div className="bg-white/10 backdrop-blur-md p-10 rounded-3xl shadow-xl w-full max-w-xl border border-white/20">
        <div className="flex items-center gap-3 mb-6">
          <Sparkles className="w-8 h-8" />
          <h1 className="text-3xl font-semibold">Fran 4.0</h1>
        </div>
        <p className="text-lg text-indigo-50 mb-8">
          Che, soy Fran de Tercom. Te ayudo con el catálogo mayorista de motopartes. Arrancá cargando tu CSV o pasate a PRO.
        </p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <button
            className="bg-white text-indigo-600 font-semibold py-3 rounded-xl shadow hover:shadow-lg transition"
            onClick={() => onEnter(AppState.UPLOAD)}
          >
            <span className="flex items-center justify-center gap-2">
              <LogIn className="w-5 h-5" /> Ingresar
            </span>
          </button>
          <button
            className="border border-white/40 text-white py-3 rounded-xl hover:bg-white/10 transition"
            onClick={() => onEnter(AppState.UPLOAD)}
          >
            Upgrade a PRO
          </button>
        </div>
      </div>
    </div>
  );
}
