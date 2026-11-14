from aiogram import Router

from . import dashboard, extra, menu, subscription
from .s import connect as s_connect
from .s.block import dialog as s_block_dialog
from .s.block import handlers as s_block_handlers
from .s.dashboard import dialog as s_dashboard_dialog


def setup_routers(router: Router) -> None:
    # WARNING: The order of router registration matters!
    routers = [
        extra.payment.router,
        extra.notification.router,
        extra.test.router,
        extra.commands.router,
        extra.member.router,
        extra.channel_member.router,
        extra.goto.router,
        extra.inline.router,
        s_block_handlers.router,
        #
        menu.handlers.router,
        menu.dialog.router,
        #
        subscription.dialog.router,
        s_connect.router,
        #
        dashboard.dialog.router,
        dashboard.statistics.dialog.router,
        dashboard.access.dialog.router,
        dashboard.broadcast.dialog.router,
        dashboard.promocodes.dialog.router,
        dashboard.remnawave.dialog.router,
        #
        dashboard.remnashop.dialog.router,
        dashboard.remnashop.gateways.dialog.router,
        dashboard.remnashop.referral.dialog.router,
        dashboard.remnashop.notifications.dialog.router,
        dashboard.remnashop.plans.dialog.router,
        dashboard.remnashop.menu_editor.dialog.router,
        dashboard.remnashop.backup.dialog.router,
        dashboard.remnashop.advertising.dialog.router,
        dashboard.remnashop.extra.dialog.router,
        s_dashboard_dialog.router,
        s_block_dialog.router,
        #
        dashboard.users.dialog.router,
        dashboard.users.user.dialog.router,
        #
        dashboard.importer.dialog.router,
    ]

    router.include_routers(*routers)
