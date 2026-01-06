import { useState } from 'react';
import { CloudUpload, Loader2 } from 'lucide-react';
import { Product } from '../types';
import { parseCsv } from '../services/csvService';

interface CsvUploaderProps {
  onUpload: (products: Product[]) => Promise<void>;
}

export default function CsvUploader({ onUpload }: CsvUploaderProps) {
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleFile = async (file?: File) => {
    if (!file) return;
    setError(null);
    setIsLoading(true);
    try {
      const products = await parseCsv(file);
      await onUpload(products);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-100 p-6">
      <div className="bg-white p-8 rounded-3xl shadow-xl border border-slate-200 w-full max-w-xl">
        <div className="flex items-center gap-3 mb-6">
          <CloudUpload className="w-7 h-7 text-indigo-500" />
          <h2 className="text-2xl font-semibold text-slate-800">Subí tu catálogo CSV</h2>
        </div>
        <p className="text-slate-600 mb-4">Columnas requeridas: code, name, price, currency, category.</p>
        <label className="block w-full border-2 border-dashed border-indigo-300 rounded-2xl p-6 text-center cursor-pointer hover:border-indigo-500 transition">
          <input
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            onChange={(e) => handleFile(e.target.files?.[0])}
          />
          {isLoading ? (
            <div className="flex items-center justify-center gap-2 text-indigo-600">
              <Loader2 className="w-5 h-5 animate-spin" /> Procesando...
            </div>
          ) : (
            <div className="text-indigo-600">Elegí el archivo y listo</div>
          )}
        </label>
        {error && <p className="text-red-500 mt-3">{error}</p>}
      </div>
    </div>
  );
}
