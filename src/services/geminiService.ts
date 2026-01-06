import { ChatMessage, Product } from '../types';

interface ChatResponse {
  text: string;
  hits: Product[];
}

export async function sendMessage(
  message: string,
  history: ChatMessage[],
  exchangeRate: number
): Promise<ChatResponse> {
  const payload = {
    message,
    history: history.map((h) => ({ role: h.role, parts: [{ text: h.text }] })),
    exchangeRate
  };

  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error('No se pudo contactar al backend');
  }

  return response.json();
}
