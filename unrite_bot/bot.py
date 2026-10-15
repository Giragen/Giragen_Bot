from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .database import ACTION_FEATURES, Database

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/unrite.sqlite3")

THREAD_NAMES = ("РП мероприятия", "Arena", "Capt", "MCL")

FEATURE_LABELS = {
    "delete_channel": "Удаление канала",
    "promote_rank": "Повышение ранга",
    "demote_rank": "Понижение ранга",
    "curator_assign": "Закрепиться / назначить куратора",
    "curator_unassign": "Открепить куратора",
    "tier_1": "Выдать Тир 1",
    "tier_2": "Выдать Тир 2",
    "tier_3": "Выдать Тир 3",
}

FEATURE_CHOICES = [
    app_commands.Choice(name="Удалить канал", value="delete_channel"),
    app_commands.Choice(name="Повысить ранг", value="promote_rank"),
    app_commands.Choice(name="Понизить ранг", value="demote_rank"),
    app_commands.Choice(name="Закрепиться", value="curator_assign"),
    app_commands.Choice(name="Открепить куратора", value="curator_unassign"),
    app_commands.Choice(name="Выдать Тир 1", value="tier_1"),
    app_commands.Choice(name="Выдать Тир 2", value="tier_2"),
    app_commands.Choice(name="Выдать Тир 3", value="tier_3"),
]
TIER_CHOICES = [
    app_commands.Choice(name="Тир 1", value=1),
    app_commands.Choice(name="Тир 2", value=2),
    app_commands.Choice(name="Тир 3", value=3),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("unrite")


def channel_slug(display_name: str, user_id: int) -> str:
    """Build a Discord-compatible channel name from a user's display name."""
    slug = re.sub(r"[^\w-]+", "-", display_name.lower(), flags=re.UNICODE)
    slug = re.sub(r"[-_]{2,}", "-", slug).strip("-_ ")
    if not slug:
        slug = f"player-{user_id}"
    return f"архи-{slug}"[:100]


def short_error(exc: Exception) -> str:
    if isinstance(exc, discord.Forbidden):
        return (
            "У бота не хватает прав Discord. Проверьте права на каналы и ветки, "
            "а также положение роли бота выше ролей тиров/рангов."
        )
    if isinstance(exc, discord.NotFound):
        return "Объект Discord уже удалён или недоступен. Обновите настройки и повторите."
    return f"Не удалось выполнить действие: `{type(exc).__name__}`. Подробности записаны в лог."


class UnriteBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(DATABASE_PATH)
        self._creation_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        await self.db.initialize()
        await self.add_cog(ArchiveCog(self))
        await self.add_cog(UnriteSettingsCog(self))
        self.add_view(CreateArchiveView(self))
        self.add_view(ArchiveControlView(self))

        try:
            if GUILD_ID:
                guild_object = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild_object)
                synced = await self.tree.sync(guild=guild_object)
                log.info("Synced %s application commands to guild %s", len(synced), GUILD_ID)
            else:
                synced = await self.tree.sync()
                log.info("Synced %s global application commands", len(synced))
        except discord.HTTPException:
            log.exception("Failed to sync application commands")

    async def on_ready(self) -> None:
        if self.user:
            log.info("Unrite is online as %s (ID: %s)", self.user, self.user.id)

    async def is_configuration_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        return (
            interaction.user.id == interaction.guild.owner_id
            or interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        )

    async def can_manage_feature(
        self, interaction: discord.Interaction, feature: str
    ) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        if (
            interaction.user.id == interaction.guild.owner_id
            or interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        ):
            return True
        role_ids = [role.id for role in interaction.user.roles]
        return await self.db.member_has_feature(interaction.guild.id, role_ids, feature)

    async def send_ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException:
            log.exception("Could not send ephemeral interaction response")

    async def fetch_member_safe(
        self, guild: discord.Guild, member_id: int
    ) -> discord.Member | None:
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def build_archive_embed(self, guild: discord.Guild, record: dict[str, Any]) -> discord.Embed:
        owner = await self.fetch_member_safe(guild, int(record["owner_id"]))
        settings = await self.db.get_guild_settings(guild.id)
        rank_roles = await self.db.list_rank_roles(guild.id)
        tier_roles = await self.db.list_tier_roles(guild.id)

        if owner:
            owner_label = f"{owner.mention} (`{owner.display_name}`)"
        else:
            owner_label = f"ID участника: `{record['owner_id']}`"

        rank_position = int(record.get("rank_position", 0))
        if rank_position:
            rank_role_id = rank_roles.get(rank_position)
            rank_role = guild.get_role(rank_role_id) if rank_role_id else None
            rank_text = rank_role.mention if rank_role else f"Ранг {rank_position} (роль не настроена)"
        else:
            rank_text = "Не назначен"

        tier = int(record.get("tier", 0))
        if tier:
            tier_role_id = tier_roles.get(tier)
            tier_role = guild.get_role(tier_role_id) if tier_role_id else None
            tier_text = tier_role.mention if tier_role else f"Тир {tier} (роль не настроена)"
        else:
            tier_text = "Нет тира"

        curator_role_id = settings.get("curator_role_id")
        curator_role = guild.get_role(curator_role_id) if curator_role_id else None
        has_curator = bool(
            owner
            and curator_role
            and any(role.id == curator_role.id for role in owner.roles)
        )
        curator_text = f"Закреплён — {curator_role.mention}" if has_curator else "Не закреплён"

        embed = discord.Embed(
            title="UNRITE • Личный архив игрока",
            description=(
                f"**Личный текстовый канал участника**\n{owner_label}\n\n"
                "▸ Прикрепляйте сюда материалы и откаты.\n"
                "▸ Используйте четыре приватные ветки для разделения активностей.\n"
                "▸ Упоминание участника выдаёт ему доступ к каналу и всем веткам."
            ),
            colour=discord.Colour.from_rgb(27, 28, 33),
        )
        embed.add_field(name="Текущий ранг", value=rank_text, inline=True)
        embed.add_field(name="Текущий тир", value=tier_text, inline=True)
        embed.add_field(name="Куратор", value=curator_text, inline=False)
        embed.add_field(
            name="Ветки канала",
            value="`РП мероприятия` · `Arena` · `Capt` · `MCL`",
            inline=False,
        )
        embed.set_footer(text="Unrite • Управление архивом доступно ролям с назначенными разрешениями")
        if owner and owner.display_avatar:
            embed.set_thumbnail(url=owner.display_avatar.url)
        return embed

    async def create_archive_for(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Создание личного архива доступно только на сервере.")
            return

        key = (guild.id, interaction.user.id)
        lock = self._creation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = await self.db.get_archive_by_owner(guild.id, interaction.user.id)
            if existing:
                existing_channel = guild.get_channel(int(existing["channel_id"]))
                if existing_channel is None:
                    try:
                        fetched = await guild.fetch_channel(int(existing["channel_id"]))
                        existing_channel = fetched if isinstance(fetched, discord.TextChannel) else None
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        existing_channel = None
                if existing_channel:
                    await self.send_ephemeral(
                        interaction,
                        f"У тебя уже есть личный архив: {existing_channel.mention}",
                    )
                    return
                await self.db.delete_archive(int(existing["channel_id"]))

            settings = await self.db.get_guild_settings(guild.id)
            category_id = settings.get("category_id")
            category = guild.get_channel(int(category_id)) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                await self.send_ephemeral(
                    interaction,
                    "Категория для архивов ещё не настроена. Администратор должен выполнить `/unrite setup`.",
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            member = interaction.user
            bot_member = guild.me
            if bot_member is None and self.user:
                bot_member = guild.get_member(self.user.id)
            if bot_member is None:
                await interaction.followup.send("Не удалось определить участника-бота на сервере.", ephemeral=True)
                return

            overwrites: dict[Any, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    send_messages_in_threads=True,
                    create_private_threads=False,
                    create_public_threads=False,
                ),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_threads=True,
                    create_private_threads=True,
                    send_messages_in_threads=True,
                    embed_links=True,
                    attach_files=True,
                ),
            }
            manager_role_ids = await self.db.manager_role_ids(guild.id)
            for role_id in manager_role_ids:
                role = guild.get_role(role_id)
                if role and role not in overwrites:
                    overwrites[role] = self.manager_overwrite()

            archive_channel: discord.TextChannel | None = None
            created_thread_ids: dict[str, int] = {}
            try:
                safe_name = channel_slug(member.display_name, member.id)
                archive_channel = await guild.create_text_channel(
                    name=safe_name,
                    category=category,
                    topic=f"Unrite | Архив игрока: {member.display_name} | ID: {member.id}",
                    overwrites=overwrites,
                    reason=f"Unrite: личный архив для {member} ({member.id})",
                )

                for thread_name in THREAD_NAMES:
                    thread = await archive_channel.create_thread(
                        name=thread_name,
                        invitable=False,
                        auto_archive_duration=10080,
                        reason=f"Unrite: приватная ветка {thread_name} для {member}",
                    )
                    await thread.add_user(member)
                    await thread.send(
                        f"**Unrite • {thread_name}**\n"
                        f"Личная ветка участника {member.mention}. Упоминание другого участника в архиве добавит его в эту ветку и остальные ветки архива."
                    )
                    created_thread_ids[thread_name] = thread.id

                await self.db.create_archive(
                    guild.id, member.id, archive_channel.id, created_thread_ids
                )
                embed = await self.build_archive_embed(
                    guild,
                    {
                        "guild_id": guild.id,
                        "owner_id": member.id,
                        "channel_id": archive_channel.id,
                        "rank_position": 0,
                        "tier": 0,
                        "curator_assigned": 0,
                        "thread_ids": created_thread_ids,
                    },
                )
                await archive_channel.send(embed=embed, view=ArchiveControlView(self))
            except Exception as exc:
                log.exception("Failed to create archive for user %s", member.id)
                if archive_channel:
                    try:
                        await archive_channel.delete(reason="Unrite: rollback after creation error")
                    except discord.HTTPException:
                        pass
                    await self.db.delete_archive(archive_channel.id)
                await interaction.followup.send(
                    f"Не удалось создать личный архив. {short_error(exc)}", ephemeral=True
                )
                return

            await interaction.followup.send(
                f"Готово! Твой личный архив: {archive_channel.mention}\n"
                "Созданы приватные ветки: РП мероприятия, Arena, Capt и MCL.",
                ephemeral=True,
            )

    @staticmethod
    def manager_overwrite() -> discord.PermissionOverwrite:
        return discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            send_messages_in_threads=True,
            manage_threads=True,
        )

    async def refresh_manager_role_access(self, guild: discord.Guild, role: discord.Role) -> None:
        allowed = await self.db.role_has_any_action_permission(guild.id, role.id)
        overwrite = self.manager_overwrite() if allowed else None
        for record in await self.db.list_archives(guild.id):
            channel = guild.get_channel(int(record["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.set_permissions(
                        role,
                        overwrite=overwrite,
                        reason="Unrite: обновление разрешений управляющей роли",
                    )
                except discord.HTTPException:
                    log.exception("Failed to update archive permissions for role %s", role.id)

    async def handle_archive_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self.send_ephemeral(interaction, "Эта панель работает только на сервере.")
            return

        channel_id: int | None = None
        if isinstance(interaction.channel, discord.TextChannel):
            channel_id = interaction.channel.id
        elif isinstance(interaction.channel, discord.Thread):
            channel_id = interaction.channel.parent_id

        record = await self.db.get_archive_by_channel(channel_id) if channel_id else None
        if not record:
            await self.send_ephemeral(interaction, "Эта панель не привязана к архиву Unrite.")
            return

        if not await self.can_manage_feature(interaction, action):
            await self.send_ephemeral(
                interaction,
                f"Нет доступа к функции **{FEATURE_LABELS.get(action, action)}**. "
                "Администратор может выдать разрешение через `/unrite permission`.",
            )
            await self.reset_control_view(interaction)
            return

        archive_channel = guild.get_channel(int(record["channel_id"]))
        if not isinstance(archive_channel, discord.TextChannel):
            await self.send_ephemeral(interaction, "Основной канал архива не найден.")
            return

        owner = await self.fetch_member_safe(guild, int(record["owner_id"]))
        if owner is None:
            await self.send_ephemeral(interaction, "Владелец архива больше не найден на сервере.")
            return

        if action == "delete_channel":
            await self.send_ephemeral(interaction, "Удаляю канал и его ветки…")
            try:
                await archive_channel.delete(reason=f"Unrite: удалено пользователем {interaction.user}")
                await self.db.delete_archive(archive_channel.id)
            except discord.HTTPException as exc:
                await interaction.followup.send(short_error(exc), ephemeral=True)
            return

        try:
            if action in {"promote_rank", "demote_rank"}:
                await self.change_rank(guild, owner, record, action)
            elif action in {"tier_1", "tier_2", "tier_3"}:
                tier = int(action[-1])
                await self.assign_tier(guild, owner, record, tier)
            elif action in {"curator_assign", "curator_unassign"}:
                await self.change_curator(guild, owner, record, action == "curator_assign")
            else:
                await self.send_ephemeral(interaction, "Неизвестное действие.")
                return

            updated = await self.db.get_archive_by_channel(archive_channel.id)
            if not updated:
                await self.send_ephemeral(interaction, "Запись архива не найдена после изменения.")
                return
            embed = await self.build_archive_embed(guild, updated)
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    embed=embed, view=ArchiveControlView(self)
                )
            else:
                await interaction.followup.send("Изменения сохранены.", ephemeral=True)
                if interaction.message:
                    await interaction.message.edit(embed=embed, view=ArchiveControlView(self))
        except ValueError as exc:
            await self.send_ephemeral(interaction, str(exc))
            await self.reset_control_view(interaction)
        except discord.Forbidden as exc:
            log.warning("Role operation forbidden: %s", exc)
            await self.send_ephemeral(
                interaction,
                "Discord запретил изменить роли. Проверьте, что роль бота находится выше ролей тиров, рангов и куратора.",
            )
            await self.reset_control_view(interaction)
        except discord.HTTPException as exc:
            log.exception("Discord API failed during archive action %s", action)
            await self.send_ephemeral(interaction, short_error(exc))
            await self.reset_control_view(interaction)

    async def change_rank(
        self,
        guild: discord.Guild,
        owner: discord.Member,
        record: dict[str, Any],
        action: str,
    ) -> None:
        configured = await self.db.list_rank_roles(guild.id)
        positions = sorted(configured)
        if not positions:
            raise ValueError("Сначала настройте роли рангов командой `/unrite rank_role`.")

        current = int(record.get("rank_position", 0))
        if action == "promote_rank":
            target_position = next((pos for pos in positions if pos > current), None)
            if current == 0:
                target_position = positions[0]
            if target_position is None:
                raise ValueError("У игрока уже самый высокий из настроенных рангов.")
        else:
            if current == 0:
                raise ValueError("У игрока ещё не назначен ранг.")
            lower_positions = [pos for pos in positions if pos < current]
            target_position = lower_positions[-1] if lower_positions else 0

        configured_role_ids = set(configured.values())
        remove_roles = [role for role in owner.roles if role.id in configured_role_ids]
        target_role = guild.get_role(configured[target_position]) if target_position else None
        if target_position and target_role is None:
            raise ValueError("Роль выбранного ранга удалена. Настройте её заново через `/unrite rank_role`.")

        if remove_roles:
            await owner.remove_roles(*remove_roles, reason="Unrite: изменение ранга")
        if target_role:
            await owner.add_roles(target_role, reason="Unrite: изменение ранга")
        await self.db.update_archive(int(record["channel_id"]), rank_position=target_position)

    async def assign_tier(
        self,
        guild: discord.Guild,
        owner: discord.Member,
        record: dict[str, Any],
        tier: int,
    ) -> None:
        target_role_id = await self.db.get_tier_role(guild.id, tier)
        target_role = guild.get_role(target_role_id) if target_role_id else None
        if target_role is None:
            raise ValueError(
                f"Роль для Тира {tier} не настроена. Администратор должен выполнить `/unrite tier_role`."
            )

        configured_roles = await self.db.list_tier_roles(guild.id)
        configured_role_ids = set(configured_roles.values())
        remove_roles = [
            role for role in owner.roles if role.id in configured_role_ids and role.id != target_role.id
        ]
        if remove_roles:
            await owner.remove_roles(*remove_roles, reason="Unrite: смена тира")
        if target_role not in owner.roles:
            await owner.add_roles(target_role, reason=f"Unrite: выдан Тир {tier}")
        await self.db.update_archive(int(record["channel_id"]), tier=tier)

    async def change_curator(
        self,
        guild: discord.Guild,
        owner: discord.Member,
        record: dict[str, Any],
        assign: bool,
    ) -> None:
        settings = await self.db.get_guild_settings(guild.id)
        role_id = settings.get("curator_role_id")
        curator_role = guild.get_role(int(role_id)) if role_id else None
        if curator_role is None:
            raise ValueError("Роль куратора не настроена. Используйте `/unrite curator_role`.")

        owner_has_role = any(role.id == curator_role.id for role in owner.roles)
        if assign and not owner_has_role:
            await owner.add_roles(curator_role, reason="Unrite: закрепление куратора")
        elif not assign and owner_has_role:
            await owner.remove_roles(curator_role, reason="Unrite: открепление куратора")
        await self.db.update_archive(
            int(record["channel_id"]), curator_assigned=int(assign)
        )

    async def reset_control_view(self, interaction: discord.Interaction) -> None:
        if interaction.message is None:
            return
        try:
            await interaction.message.edit(view=ArchiveControlView(self))
        except discord.HTTPException:
            log.debug("Unable to reset select menu after interaction")


class CreateArchiveView(discord.ui.View):
    def __init__(self, bot: UnriteBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Создать личный канал",
        style=discord.ButtonStyle.primary,
        emoji="📁",
        custom_id="unrite:create_archive",
    )
    async def create_archive(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.bot.create_archive_for(interaction)


class ArchiveActionSelect(discord.ui.Select):
    def __init__(self, bot: UnriteBot) -> None:
        self.bot = bot
        options = [
            discord.SelectOption(label="Удалить канал", value="delete_channel", description="Удалить личный архив"),
            discord.SelectOption(label="Повышение ранга", value="promote_rank", description="Выдать следующий настроенный ранг"),
            discord.SelectOption(label="Понижение ранга", value="demote_rank", description="Снять ранг или понизить на один уровень"),
            discord.SelectOption(label="Закрепиться", value="curator_assign", description="Назначить настроенную роль куратора"),
            discord.SelectOption(label="Открепить куратора", value="curator_unassign", description="Снять роль куратора"),
        ]
        super().__init__(
            placeholder="Взаимодействие с каналом",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="unrite:archive_actions",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_archive_action(interaction, self.values[0])


class TierSelect(discord.ui.Select):
    def __init__(self, bot: UnriteBot) -> None:
        self.bot = bot
        options = [
            discord.SelectOption(label="Выдать Тир 1", value="tier_1", description="Назначить роль, выбранную для Тира 1"),
            discord.SelectOption(label="Выдать Тир 2", value="tier_2", description="Назначить роль, выбранную для Тира 2"),
            discord.SelectOption(label="Выдать Тир 3", value="tier_3", description="Назначить роль, выбранную для Тира 3"),
        ]
        super().__init__(
            placeholder="Выдача тира",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="unrite:tier_actions",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_archive_action(interaction, self.values[0])


class ArchiveControlView(discord.ui.View):
    def __init__(self, bot: UnriteBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(ArchiveActionSelect(bot))
        self.add_item(TierSelect(bot))


class ArchiveCog(commands.Cog):
    def __init__(self, bot: UnriteBot) -> None:
        self.bot = bot

    @app_commands.command(name="archi", description="Создать личный архив Unrite с четырьмя приватными ветками")
    async def archi(self, interaction: discord.Interaction) -> None:
        await self.bot.create_archive_for(interaction)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not message.mentions:
            return

        if isinstance(message.channel, discord.Thread):
            parent_id = message.channel.parent_id
        elif isinstance(message.channel, discord.TextChannel):
            parent_id = message.channel.id
        else:
            return

        record = await self.bot.db.get_archive_by_channel(parent_id) if parent_id else None
        if not record:
            return

        parent = message.guild.get_channel(int(record["channel_id"]))
        if not isinstance(parent, discord.TextChannel):
            return

        thread_ids = record.get("thread_ids", {})
        for mentioned_member in message.mentions:
            if mentioned_member.bot or mentioned_member.id == int(record["owner_id"]):
                continue
            try:
                await parent.set_permissions(
                    mentioned_member,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                        embed_links=True,
                        send_messages_in_threads=True,
                        create_private_threads=False,
                        create_public_threads=False,
                    ),
                    reason=f"Unrite: доступ выдан через упоминание от {message.author}",
                )
                for thread_id in thread_ids.values():
                    try:
                        thread = message.guild.get_thread(int(thread_id))
                        if thread is None:
                            fetched = await message.guild.fetch_channel(int(thread_id))
                            thread = fetched if isinstance(fetched, discord.Thread) else None
                        if thread is None:
                            continue
                        if thread.archived:
                            await thread.edit(archived=False, reason="Unrite: добавление упомянутого участника")
                        await thread.add_user(mentioned_member)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        log.exception(
                            "Could not add mentioned member %s to an archive thread %s",
                            mentioned_member.id,
                            thread_id,
                        )
                log.info(
                    "Granted archive %s access to mentioned member %s",
                    parent.id,
                    mentioned_member.id,
                )
            except discord.HTTPException:
                log.exception(
                    "Could not grant archive %s access to mentioned member %s",
                    parent.id,
                    mentioned_member.id,
                )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        record = await self.bot.db.get_archive_by_channel(channel.id)
        if record:
            await self.bot.db.delete_archive(channel.id)


class UnriteSettingsCog(commands.GroupCog, group_name="unrite", group_description="Настройки и управление ботом Unrite"):
    def __init__(self, bot: UnriteBot) -> None:
        self.bot = bot

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if not await self.bot.is_configuration_admin(interaction):
            await self.bot.send_ephemeral(
                interaction,
                "Эта настройка доступна владельцу сервера, администраторам или участникам с правом `Manage Server`.",
            )
            return False
        return True

    @app_commands.command(name="setup", description="Указать категорию, где бот создаёт личные архивы")
    @app_commands.describe(category="Категория для новых архивов")
    async def setup(self, interaction: discord.Interaction, category: discord.CategoryChannel) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return
        await self.bot.db.set_guild_setting(interaction.guild.id, "category_id", category.id)
        await interaction.response.send_message(
            f"Категория архивов Unrite установлена: {category.mention}.", ephemeral=True
        )

    @app_commands.command(name="panel", description="Опубликовать панель создания личного архива")
    @app_commands.describe(channel="Текстовый канал, в котором разместить панель")
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return
        embed = discord.Embed(
            title="UNRITE • Получение ранга и Tier",
            description=(
                "📁 **Создание личного архива**\n"
                "Нажмите кнопку ниже, чтобы создать персональный приватный канал.\n\n"
                "Внутри будут четыре закрытые ветки: **РП мероприятия**, **Arena**, **Capt** и **MCL**.\n"
                "По умолчанию архив виден владельцу и управляющим ролям. Упомяните участника, чтобы выдать ему доступ к архиву и всем веткам."
            ),
            colour=discord.Colour.from_rgb(27, 28, 33),
        )
        embed.set_footer(text="Unrite • Личные архивы, ранги и тиры")
        await channel.send(embed=embed, view=CreateArchiveView(self.bot))
        await self.bot.db.set_guild_setting(interaction.guild.id, "panel_channel_id", channel.id)
        await interaction.response.send_message(f"Панель Unrite опубликована в {channel.mention}.", ephemeral=True)

    @app_commands.command(name="permission", description="Разрешить или запретить роли отдельную функцию управления")
    @app_commands.describe(
        role="Роль, которой настраивается доступ",
        function="Какую функцию разрешить этой роли",
        allowed="Включить или выключить доступ",
    )
    @app_commands.choices(function=FEATURE_CHOICES)
    async def permission(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        function: app_commands.Choice[str],
        allowed: bool,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return
        if role.is_default():
            await self.bot.send_ephemeral(interaction, "Нельзя назначить функции управления роли `@everyone`.")
            return
        await self.bot.db.set_permission(interaction.guild.id, role.id, function.value, allowed)
        await self.bot.refresh_manager_role_access(interaction.guild, role)
        state = "разрешена" if allowed else "запрещена"
        await interaction.response.send_message(
            f"Функция **{FEATURE_LABELS[function.value]}** для роли {role.mention} теперь **{state}**.",
            ephemeral=True,
        )

    @app_commands.command(name="tier_role", description="Выбрать роль, которая выдаётся за Тир 1, 2 или 3")
    @app_commands.describe(tier="Уровень тира", role="Discord-роль для этого тира")
    @app_commands.choices(tier=TIER_CHOICES)
    async def tier_role(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[int],
        role: discord.Role,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return
        if role.is_default() or role.managed:
            await self.bot.send_ephemeral(interaction, "Выберите обычную серверную роль, которую бот сможет выдавать.")
            return
        await self.bot.db.set_tier_role(interaction.guild.id, tier.value, role.id)
        await interaction.response.send_message(
            f"Для **Тира {tier.value}** теперь используется роль {role.mention}.", ephemeral=True
        )

    @app_commands.command(name="rank_role", description="Настроить роль для позиции ранга")
    @app_commands.describe(position="Позиция от 1 до 20; чем больше число, тем выше ранг", role="Роль этого ранга")
    async def rank_role(
        self,
        interaction: discord.Interaction,
        position: app_commands.Range[int, 1, 20],
        role: discord.Role,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return
        if role.is_default() or role.managed:
            await self.bot.send_ephemeral(interaction, "Выберите обычную серверную роль, которую бот сможет выдавать.")
            return
        await self.bot.db.set_rank_role(interaction.guild.id, int(position), role.id)
        await interaction.response.send_message(
            f"Для позиции ранга **{position}** установлена роль {role.mention}. Следующие повышения пойдут к следующей настроенной позиции.",
            ephemeral=True,
        )

    @app_commands.command(name="curator_role", description="Выбрать роль для действия «Закрепиться»")
    @app_commands.describe(role="Роль куратора, которая будет назначаться игроку")
    async def curator_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return
        if role.is_default() or role.managed:
            await self.bot.send_ephemeral(interaction, "Выберите обычную серверную роль, которую бот сможет выдавать.")
            return
        await self.bot.db.set_guild_setting(interaction.guild.id, "curator_role_id", role.id)
        await interaction.response.send_message(
            f"Роль куратора установлена: {role.mention}.", ephemeral=True
        )

    @app_commands.command(name="status", description="Показать текущие настройки Unrite")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        if interaction.guild is None:
            await self.bot.send_ephemeral(interaction, "Команда доступна только на сервере.")
            return

        guild = interaction.guild
        settings = await self.bot.db.get_guild_settings(guild.id)
        category = guild.get_channel(settings.get("category_id") or 0)
        panel_channel = guild.get_channel(settings.get("panel_channel_id") or 0)
        curator = guild.get_role(settings.get("curator_role_id") or 0)
        tier_roles = await self.bot.db.list_tier_roles(guild.id)
        rank_roles = await self.bot.db.list_rank_roles(guild.id)
        permissions = await self.bot.db.list_permissions(guild.id)

        embed = discord.Embed(title="UNRITE • Настройки", colour=discord.Colour.blurple())
        embed.add_field(name="Категория архивов", value=category.mention if category else "Не настроена", inline=False)
        embed.add_field(name="Панель создания", value=panel_channel.mention if panel_channel else "Не опубликована", inline=False)
        embed.add_field(name="Роль куратора", value=curator.mention if curator else "Не настроена", inline=False)
        embed.add_field(
            name="Роли тиров",
            value="\n".join(
                f"Тир {tier}: {guild.get_role(role_id).mention if guild.get_role(role_id) else '`роль удалена`'}"
                for tier, role_id in sorted(tier_roles.items())
            ) or "Не настроены",
            inline=False,
        )
        embed.add_field(
            name="Роли рангов",
            value="\n".join(
                f"Позиция {position}: {guild.get_role(role_id).mention if guild.get_role(role_id) else '`роль удалена`'}"
                for position, role_id in sorted(rank_roles.items())
            ) or "Не настроены",
            inline=False,
        )
        enabled_permissions = [item for item in permissions if item["allowed"]]
        if enabled_permissions:
            lines: list[str] = []
            for item in enabled_permissions:
                role = guild.get_role(int(item["role_id"]))
                role_text = role.mention if role else f"роль `{item['role_id']}` удалена"
                lines.append(f"{role_text} — {FEATURE_LABELS.get(item['feature'], item['feature'])}")
            embed.add_field(name="Разрешения управления", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Разрешения управления", value="Дополнительные роли не назначены; управлять могут администраторы.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def main() -> None:
    if not TOKEN or TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Не задан DISCORD_TOKEN. Создайте файл .env на основе .env.example и вставьте токен Discord-бота."
        )
    bot = UnriteBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Unrite stopped")
