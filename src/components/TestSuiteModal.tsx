import { useState } from 'react';
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { ChatMessage, Product } from '../types';
import { sendMessage } from '../services/geminiService';

interface TestSuiteModalProps {
  isOpen: boolean;
  onClose: () => void;
  exchangeRate: number;
}

interface TestCaseResult {
  name: string;
  status: 'pending' | 'running' | 'passed' | 'failed';
  response?: string;
}

const PREDEFINED_TESTS = [
  { name: 'Saludo', prompt: 'Hola Fran, ¿todo bien?' },
  { name: 'Búsqueda exacta', prompt: 'Tenés el código ABC123?' },
  { name: 'Búsqueda parcial', prompt: 'Necesito pastillas de freno Honda' },
  { name: 'Pregunta genérica', prompt: '¿Qué medios de pago aceptan?' },
  { name: 'Código inexistente', prompt: 'Buscá el código XYZ9999' }
];

export default function TestSuiteModal({ isOpen, onClose, exchangeRate }: TestSuiteModalProps) {
  const [results, setResults] = useState<TestCaseResult[]>(
    PREDEFINED_TESTS.map((test) => ({ name: test.name, status: 'pending' as const }))
  );
  const [isRunning, setIsRunning] = useState(false);

  const runTests = async () => {
    setIsRunning(true);
    const newResults: TestCaseResult[] = [];
    for (const test of PREDEFINED_TESTS) {
      newResults.push({ name: test.name, status: 'running' });
      setResults([...newResults, ...PREDEFINED_TESTS.slice(newResults.length).map((t) => ({ name: t.name, status: 'pending' as const }))]);
      try {
        const response = await sendMessage(test.prompt, [], exchangeRate);
        newResults[newResults.length - 1] = { name: test.name, status: 'passed', response: response.text };
      } catch (error) {
        newResults[newResults.length - 1] = {
          name: test.name,
          status: 'failed',
          response: (error as Error).message
        };
      }
    }
    setResults(newResults);
    setIsRunning(false);
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-2xl">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-xl font-semibold text-slate-800">Suite de pruebas</h3>
          <button onClick={onClose} className="text-slate-500">Cerrar</button>
        </div>
        <p className="text-slate-600 mb-4">Incluye saludo, búsquedas exactas/parciales, consulta genérica y código inexistente.</p>
        <button
          className="mb-4 bg-indigo-600 text-white px-4 py-2 rounded-xl hover:bg-indigo-700 disabled:opacity-60"
          onClick={runTests}
          disabled={isRunning}
        >
          {isRunning ? <Loader2 className="w-4 h-4 animate-spin inline-block" /> : 'Ejecutar pruebas'}
        </button>
        <div className="space-y-3 max-h-96 overflow-y-auto">
          {results.map((result) => (
            <div key={result.name} className="border border-slate-200 rounded-xl p-3">
              <div className="flex items-center gap-2">
                {result.status === 'passed' && <CheckCircle2 className="w-5 h-5 text-green-600" />}
                {result.status === 'failed' && <XCircle className="w-5 h-5 text-red-500" />}
                {result.status === 'running' && <Loader2 className="w-5 h-5 animate-spin text-indigo-500" />}
                {result.status === 'pending' && <div className="w-5 h-5 rounded-full border border-slate-200" />}
                <span className="font-semibold text-slate-800">{result.name}</span>
                <span className="text-xs uppercase text-slate-500">{result.status}</span>
              </div>
              {result.response && <div className="mt-2 text-slate-700 whitespace-pre-wrap">{result.response}</div>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
