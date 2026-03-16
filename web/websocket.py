"""
WebSocket Handler for Live Chat Interface

Manages WebSocket connections for real-time communication
between the trading bot and the web UI.
"""

import asyncio
import json
from typing import Set, Dict, Any, Optional
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger


class ConnectionManager:
    """
    Manages WebSocket connections.
    
    Handles:
    - Connection/disconnection
    - Broadcasting messages to all clients
    - Sending messages to specific clients
    """
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._message_history: list = []
        self._max_history = 100
    
    async def connect(self, websocket: WebSocket):
        """Accept and register a new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"Client connected. Total connections: {len(self.active_connections)}")
        
        # Send connection confirmation
        await self.send_personal(websocket, {
            "type": "system",
            "message": "Connected to trading bot",
            "timestamp": datetime.now().isoformat()
        })
        
        # Send recent message history
        for msg in self._message_history[-20:]:
            await self.send_personal(websocket, msg)
    
    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection"""
        self.active_connections.discard(websocket)
        logger.info(f"Client disconnected. Total connections: {len(self.active_connections)}")
    
    async def send_personal(self, websocket: WebSocket, message: Dict[str, Any]):
        """Send message to a specific client"""
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.error(f"Error sending to client: {e}")
            self.disconnect(websocket)
    
    async def broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected clients"""
        # Add to history
        self._message_history.append(message)
        if len(self._message_history) > self._max_history:
            self._message_history = self._message_history[-self._max_history:]
        
        # Send to all connections
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Broadcast error: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)
    
    async def broadcast_text(self, text: str, msg_type: str = "info"):
        """Broadcast a text message"""
        await self.broadcast({
            "type": msg_type,
            "message": text,
            "timestamp": datetime.now().isoformat()
        })
    
    def get_connection_count(self) -> int:
        """Get number of active connections"""
        return len(self.active_connections)


class ChatHandler:
    """
    Handles chat message processing.
    
    Routes messages between the web UI and the trading bot.
    """
    
    def __init__(self, connection_manager: ConnectionManager):
        self.manager = connection_manager
        self._bot = None  # Set by app.py
        self._command_history: list = []
    
    def set_bot(self, bot):
        """Set the trading bot instance"""
        self._bot = bot
        
        # Register callback for bot messages
        if bot:
            bot.register_message_callback(self._handle_bot_message)
    
    async def _handle_bot_message(self, message: Dict[str, Any]):
        """Handle messages from the trading bot"""
        await self.manager.broadcast(message)
    
    async def handle_message(self, websocket: WebSocket, data: Dict[str, Any]):
        """
        Handle incoming WebSocket message.
        
        Expected message format:
        {
            "type": "command" | "chat",
            "content": "the message content"
        }
        """
        msg_type = data.get("type", "chat")
        content = data.get("content", "").strip()
        
        if not content:
            return
        
        # Log the incoming message
        logger.info(f"Received: {msg_type} - {content}")
        
        # Echo the user's message
        await self.manager.broadcast({
            "type": "user",
            "message": content,
            "timestamp": datetime.now().isoformat()
        })
        
        # Process based on type
        if msg_type == "command" or content.startswith("/"):
            # Strip leading slash if present
            cmd = content[1:] if content.startswith("/") else content
            await self._process_command(cmd)
        else:
            # Treat regular messages as commands too for simplicity
            await self._process_command(content)
    
    async def _process_command(self, command: str):
        """Process a command and send response"""
        # Add to history
        self._command_history.append({
            "command": command,
            "timestamp": datetime.now().isoformat()
        })
        
        if not self._bot:
            await self.manager.broadcast({
                "type": "error",
                "message": "Bot not initialized. Please wait...",
                "timestamp": datetime.now().isoformat()
            })
            return
        
        try:
            # Process command through bot
            response = await self._bot.process_command(command)
            
            # Send response
            await self.manager.broadcast({
                "type": "bot",
                "message": response,
                "timestamp": datetime.now().isoformat()
            })
            
        except Exception as e:
            logger.error(f"Command processing error: {e}")
            await self.manager.broadcast({
                "type": "error",
                "message": f"Error: {str(e)}",
                "timestamp": datetime.now().isoformat()
            })
    
    def get_command_history(self) -> list:
        """Get recent command history"""
        return self._command_history[-50:]


# Global instances
manager = ConnectionManager()
chat_handler = ChatHandler(manager)


async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint handler.
    
    Used by FastAPI to handle WebSocket connections.
    """
    await manager.connect(websocket)
    
    try:
        while True:
            # Receive message
            data = await websocket.receive_json()
            
            # Handle message
            await chat_handler.handle_message(websocket, data)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logger.info("Client disconnected gracefully")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
