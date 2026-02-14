import asyncclick as click

COMMAND_ALIASES = {
    "list": "ls",
    "upl": "up",
    "upload": "up",
    "down": "dl",
    "download": "dl",
    "delete": "rm",
    "del": "rm",
    "remove": "rm",
}


class AliasedCommands(click.Group):
    """Click group with command alias support."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Get command by name or alias.

        Args:
            ctx: Click context.
            cmd_name: Command name or alias.

        Returns:
            The command if found, None otherwise.
        """
        rv = click.Group.get_command(self, ctx, cmd_name)
        if rv is not None:
            return rv
        try:
            return click.Group.get_command(self, ctx, COMMAND_ALIASES[cmd_name])
        except KeyError:
            return None
