"""
Base Discord bot implementation for AI Zoo.
This serves as a foundation for all bot implementations.
"""
import os
import logging
import asyncio
import random
import discord
from discord.ext import commands
from typing import Optional, Dict, Any, List

from utils.config_loader import get_env, load_env_vars
from utils.conversation import ConversationManager
from utils.random_delay import delay_response, simulate_typing
from services.llm_service import LLMService
from services.notion_service import NotionService

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaseDiscordBot(commands.Bot):
    """Base Discord bot for AI Zoo."""
    
    def __init__(self, 
                character_name: str,
                command_prefix: str = "!",
                intents: Optional[discord.Intents] = None):
        """
        Initialize the base bot.
        
        Args:
            character_name: Name of the character this bot will play
            command_prefix: Command prefix for bot commands
            intents: Discord intents for the bot
        """
        # Set up intents
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.messages = True
            
        super().__init__(command_prefix=command_prefix, intents=intents)
        
        # Bot configuration
        self.notion_character_name = character_name  # 追加: Notion上でのキャラクター名
        self.character_name = character_name  # Discord上での表示名として使用
        self.channel_id = int(os.environ.get('CHANNEL_ID', '0'))
        self.min_response_delay = int(os.environ.get('MIN_RESPONSE_DELAY', '5'))
        self.max_response_delay = int(os.environ.get('MAX_RESPONSE_DELAY', '15'))
        self.max_conversation_turns = int(os.environ.get('MAX_CONVERSATION_TURNS', '10'))
        
        # 基本ロールスクリプトのパス
        self.base_role_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 
            'config', 
            'base_role.txt'
        )
        
        # 基本ロールの読み込み
        self.base_role = self._load_base_role()
        
        # Initialize services
        self.llm_service = LLMService()
        self.notion_service = NotionService()
        
        # Initialize conversation manager
        self.conversation_manager = ConversationManager()
        
        # Character settings
        self.character: Optional[Dict[str, Any]] = None
        self.system_prompt: Optional[str] = None
        
        # Cooldown tracking
        self.in_cooldown = False
        self.cooldown_until = 0
        
        # Register event handlers
        self.setup_events()
    
    def _load_base_role(self) -> str:
        """基本ロールスクリプトを読み込む"""
        try:
            with open(self.base_role_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"基本ロールスクリプトの読み込みに失敗: {e}")
            return "あなたはDiscordチャットに参加するAIボットです。会話の流れに自然に応答し、常にキャラクターを維持してください。"
    
    def setup_events(self):
        """Set up Discord event handlers."""
        @self.event
        async def on_ready():
            logger.info(f"Bot logged in as {self.user}")
            
            # Load character settings from Notion
            await self.load_character_settings()
            
            # Join the specified channel
            if self.channel_id:
                channel = self.get_channel(self.channel_id)
                if channel:
                    logger.info(f"Joined channel: {channel.name}")
                    
                    # Generate and send introduction message
                    intro_message = self.generate_introduction_message()
                    await channel.send(intro_message)
                    logger.info(f"Sent introduction message to channel: {channel.name}")
                else:
                    logger.error(f"Could not find channel with ID: {self.channel_id}")
        
        @self.event
        async def on_message(message):
            # Ignore messages from self
            if message.author == self.user:
                return
                
            # Only respond in the specified channel
            if self.channel_id and message.channel.id != self.channel_id:
                return
                
            # Process commands first
            await self.process_commands(message)
            
            # Don't respond to commands
            if message.content.startswith(self.command_prefix):
                return
                
            # Check if we're in cooldown
            if self.in_cooldown:
                return
                
            # Add message to conversation history
            self.conversation_manager.add_message(
                author=message.author.display_name,
                content=message.content
            )
            
            # Check if we should respond based on conversation turns
            if self.conversation_manager.should_cool_down(self.max_conversation_turns):
                logger.info(f"Cooling down after {self.max_conversation_turns} conversation turns")
                self.in_cooldown = True
                self.conversation_manager.reset_conversation_turns()
                
                # Set cooldown for a random time between 1-3 minutes
                cooldown_minutes = random.randint(1, 3)
                self.cooldown_until = asyncio.get_event_loop().time() + (cooldown_minutes * 60)
                
                # Schedule cooldown reset
                asyncio.create_task(self.reset_cooldown_after(cooldown_minutes))
                return
            
            # 子クラスで実装するメソッドを呼び出す（フックメソッド）
            if self.should_respond_to_message(message):
                # Generate and send response
                asyncio.create_task(self.respond_to_message(message))
    
    # フックメソッド - 子クラスでオーバーライド可能
    def should_respond_to_message(self, message) -> bool:
        """
        Determine if the bot should respond to a message.
        Override this in subclasses to implement custom response logic.
        
        Args:
            message: Discord message to check
            
        Returns:
            True if the bot should respond, False otherwise
        """
        return True
    
    async def load_character_settings(self):
        """Load character settings from Notion."""
        logger.info(f"Looking up character settings for Notion character name: {self.notion_character_name}")
        
        try:
            # Get character settings from Notion using the Notion character name
            self.character = await self.notion_service.get_character(self.notion_character_name)
            
            if not self.character:
                logger.warning(f"Character {self.notion_character_name} not found in Notion. Using default settings.")
                self.character = {
                    "name": self.notion_character_name,
                    "personality": "Friendly and helpful",
                    "speaking_style": "Casual and conversational",
                    "language": "English",
                    "model": "gpt-4"
                }
            
            # Format character settings into system prompt with base role
            self.system_prompt = self.notion_service.format_character_prompt(
                self.character,
                base_role=self.base_role
            )
            
            actual_name = self.character.get("name", self.notion_character_name)
            logger.info(f"Character settings loaded successfully:")
            logger.info(f"- Discord display name: {self.character_name}")
            logger.info(f"- Notion character name: {self.notion_character_name}")
            logger.info(f"- Actual name from Notion: {actual_name}")
            logger.info(f"- Model: {self.character.get('model', 'unknown')}")
            logger.info(f"- Base role loaded: {bool(self.base_role)}")
            
        except Exception as e:
            logger.error(f"Failed to load character settings: {e}")
            # Set default character settings
            self.character = {
                "name": self.notion_character_name,
                "personality": "Friendly and helpful",
                "speaking_style": "Casual and conversational",
                "language": "English",
                "model": "gpt-4"
            }
            # Use base role if available, otherwise use a simple default prompt
            if self.base_role:
                self.system_prompt = f"You are {self.notion_character_name}.\n\n{self.base_role}"
            else:
                self.system_prompt = f"You are {self.notion_character_name}. Be friendly and helpful."
    
    async def respond_to_message(self, message):
        """
        Generate and send a response to a message.
        
        Args:
            message: Discord message to respond to
        """
        try:
            # Add random delay to simulate thinking/typing
            await delay_response(self.min_response_delay, self.max_response_delay)
            
            # Get model from character settings or default to gpt-4
            model = self.character.get("model", "gpt-4") if self.character else "gpt-4"
            
            # 最後のメッセージの送信者に基づいて、システムプロンプトを動的に調整
            adjusted_system_prompt = self._adjust_system_prompt_for_sender(message.author.display_name)
            
            # Format conversation history for LLM
            if model.startswith("gpt"):
                messages = self.conversation_manager.format_for_openai(adjusted_system_prompt)
            else:
                messages = self.conversation_manager.format_for_anthropic(adjusted_system_prompt)
            
            # Simulate typing
            message_length = random.randint(50, 200)  # Estimate response length
            await simulate_typing(message.channel, message_length)
            
            # Generate response from LLM
            response = await self.llm_service.generate_response(
                messages=messages,
                model=model
            )
            
            # Add bot's response to conversation history
            self.conversation_manager.add_message(
                author=self.character_name,
                content=response,
                bot_name=self.character_name
            )
            
            # Send response
            await message.channel.send(response)
            
        except Exception as e:
            logger.error(f"Failed to respond to message: {e}")
            
    def _adjust_system_prompt_for_sender(self, sender_name: str) -> str:
        """
        送信者に基づいてシステムプロンプトを調整する
        
        Args:
            sender_name: メッセージの送信者名
            
        Returns:
            調整されたシステムプロンプト
        """
        # 基本のシステムプロンプト
        adjusted_prompt = self.system_prompt
        
        # 送信者が他のボットかどうかを確認
        bot_names = ['gpt-4o-animal', 'claude-animal', 'gpt-4o', 'claude']
        if any(bot_name.lower() in sender_name.lower() for bot_name in bot_names):
            # 他のボットからのメッセージに応答する場合の追加指示
            bot_response_guidance = """
            
現在、あなたは他のAIボットからの質問に応答しています。以下のガイドラインに従ってください：
1. 「ご指摘の通りですね」「おっしゃる通りです」などの同意から始めないでください
2. 質問に直接答え、相手の発言内容を先に知っていたかのような表現は避けてください
3. 自分の考えや意見を述べる際は、「私は〜と考えます」「私の見解では〜」などの表現を使ってください
4. 会話の自然な流れを維持しつつ、不自然な「先読み」を避けてください
"""
            adjusted_prompt += bot_response_guidance
            
        return adjusted_prompt
    
    async def reset_cooldown_after(self, minutes: int):
        """
        Reset cooldown after specified minutes.
        
        Args:
            minutes: Number of minutes to cooldown
        """
        logger.info(f"Cooling down for {minutes} minutes")
        await asyncio.sleep(minutes * 60)
        self.in_cooldown = False
        logger.info("Cooldown ended")
    
    def generate_introduction_message(self) -> str:
        """
        Generate bot introduction message based on character settings.
        
        Returns:
            Formatted introduction message
        """
        intro_parts = []
        
        # Add greeting
        intro_parts.append(f"こんにちは！私は{self.character_name}です。")
        
        # Add personality
        if self.character and "personality" in self.character:
            intro_parts.append(f"性格: {self.character['personality']}")
        
        # Add speaking style
        if self.character and "speaking_style" in self.character:
            intro_parts.append(f"話し方: {self.character['speaking_style']}")
        
        # Add interests
        if self.character and "interests" in self.character:
            interests = self.character["interests"]
            if isinstance(interests, list):
                interests_str = "、".join(interests)
            else:
                interests_str = interests
            intro_parts.append(f"興味・関心: {interests_str}")
        
        # Add background if available
        if self.character and "background" in self.character:
            intro_parts.append(f"背景: {self.character['background']}")
        
        # Add model info
        if self.character and "model" in self.character:
            intro_parts.append(f"使用モデル: {self.character['model']}")
        
        # 子クラスで追加情報を設定できるようにするためのフックメソッド
        additional_info = self.get_additional_introduction_info()
        if additional_info:
            intro_parts.append(additional_info)
        
        # Add invitation to chat
        intro_parts.append("気軽に話しかけてください！")
        
        return "\n".join(intro_parts)
    
    # フックメソッド - 子クラスでオーバーライド可能
    def get_additional_introduction_info(self) -> Optional[str]:
        """
        Get additional information for the introduction message.
        Override this in subclasses to add custom introduction information.
        
        Returns:
            Additional information string or None
        """
        return None
    
    async def send_scheduled_message(self, message: str):
        """
        Send a scheduled message to the channel.
        
        Args:
            message: Message to send
        """
        if not self.channel_id:
            logger.error("No channel ID specified for scheduled message")
            return
            
        channel = self.get_channel(self.channel_id)
        if not channel:
            logger.error(f"Could not find channel with ID: {self.channel_id}")
            return
            
        await channel.send(message)
        logger.info(f"Sent scheduled message: {message}")
