// Adapted from Vercel ai-chatbot components/chat-list.tsx (Apache-2.0).
import React from "react";
import { ChatMessage } from "./chat-message";
export function ChatList({ turns, ...props }) {
  return (
    <div className="chat-list">
      {turns.map((turn, index) => (
        <ChatMessage
          key={turn.id}
          turn={turn}
          isCurrent={index === turns.length - 1}
          {...props}
        />
      ))}
    </div>
  );
}
