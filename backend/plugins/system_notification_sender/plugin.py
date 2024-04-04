import json
from sys import modules as sys_modules
from models.api import Query
from models.enums import ContentType, QueryType, ResourceType
from models.core import (
    ActionType,
    EntityDTO,
    Notification,
    NotificationData,
    PluginBase,
    Event,
    Translation,
)
from utils.notification import NotificationManager

# from plugins.web_notification import WebNotifier, websocket_push
from utils.helpers import branch_path, camel_case, replace_message_vars

# from utils.notification import NotificationContext, send_notification
from utils.repository import internal_save_model, get_entry_attachments, get_group_users
from utils.settings import settings
from fastapi.logger import logger
from utils.db import load, load_resource_payload
from utils.operational_repo import operational_repo

class Plugin(PluginBase):
    async def hook(self, data: Event):
        """
        after any action
        1- get the matching SystemNotificationRequest for this action
        2- generate list of users to send the notification to (based on the action entry):
            2.1- entry.owner_shortname, entry.group.members?, and entry.collaborators?
        3- send the notification to the list of generated users
        """
        # Type narrowing for PyRight
        if not isinstance(data.shortname, str):
            logger.warning(
                "data.shortname is None and str is required at system_notification_sender"
            )
            return

        if data.action_type == ActionType.delete and data.attributes.get("entry"):
            entry = data.attributes["entry"].model_dump()
        else:
            entry = (
                await load(
                    data.space_name,
                    data.subpath,
                    data.shortname,
                    getattr(sys_modules["models.core"], camel_case(data.resource_type)),
                    data.user_shortname,
                    data.branch_name,
                )
            ).model_dump()
            if (
                entry["payload"]
                and entry["payload"]["content_type"] == ContentType.json
                and entry["payload"]["body"]
            ):
                entry["payload"]["body"] = load_resource_payload(
                    space_name=data.space_name,
                    subpath=data.subpath,
                    filename=entry["payload"]["body"],
                    class_type=getattr(
                        sys_modules["models.core"], camel_case(data.resource_type)
                    ),
                    branch_name=data.branch_name,
                )
        entry["space_name"] = data.space_name
        entry["resource_type"] = str(data.resource_type)
        entry["subpath"] = data.subpath
        entry["branch_name"] = str(data.branch_name)

        # 1- get the matching SystemNotificationRequests
        search_subpaths = list(filter(None, data.subpath.split("/")))
        matching_notification_requests = await operational_repo.search(Query(
            type=QueryType.search,
            space_name=settings.management_space,
            branch_name=settings.management_space_branch,
            subpath="notifications/system",
            filter_schema_names=["system_notification_request"],
            search=f"@on_space:{data.space_name} @on_subpath:({'|'.join(search_subpaths)}) @on_action:{data.action_type}",
            limit=30,
            offset=0,
        ))
        if not matching_notification_requests[0] == 0:
            return

        # 2- get list of subscribed users
        notification_subscribers = [entry["owner_shortname"]]
        # if entry.get("collaborators", None):
        #     notification_subscribers.extend(entry["collaborators"].values())  # type: ignore
        if entry.get("owner_group_shortname", None):
            group_users = await get_group_users(entry["owner_group_shortname"])
            group_members = [
                json.loads(user_doc)["shortname"] for user_doc in group_users
            ]
            notification_subscribers.extend(group_members)

        if data.user_shortname in notification_subscribers:
            notification_subscribers.remove(data.user_shortname)

        users_objects: dict[str, dict] = {}
        for subscriber in notification_subscribers:
                users_objects[subscriber] = await operational_repo.find_by_id(
                    await operational_repo.entity_doc_id(EntityDTO(
                        space_name=settings.management_space,
                        subpath=settings.users_subpath,
                        shortname=subscriber,
                        resource_type=ResourceType.user,
                        branch_name=settings.management_space_branch,
                        schema_shortname="meta",
                        
                    ))
                )

        # 3- send the notification
        notification_manager = NotificationManager()
        for notification_dict in matching_notification_requests[1]:
            if (
                "state" in entry
                and notification_dict.get("on_state", "") != ""
                and notification_dict["on_state"] != entry["state"]
            ):
                continue

            formatted_req = await self.prepare_request(notification_dict, entry)
            for receiver in set(notification_subscribers):
                if not formatted_req["push_only"]:
                    notification_obj = await Notification.from_request(
                        notification_dict, entry
                    )
                    await internal_save_model(
                        "personal",
                        f"people/{receiver}/notifications",
                        notification_obj,
                        notification_dict["branch_name"],
                    )

                for platform in formatted_req["platforms"]:
                    await notification_manager.send(
                        platform=platform,
                        data=NotificationData(
                            receiver=users_objects[receiver],
                            title=formatted_req["title"],
                            body=formatted_req["body"],
                            image_urls=formatted_req["images_urls"],
                            deep_link=notification_dict.get("deep_link", {}),
                            entry_id=entry["shortname"],
                        ),
                    )

    async def prepare_request(self, notification_dict: dict, entry: dict) -> dict:
        for locale in ["ar", "en", "ku"]:
            notification_dict["displayname"][locale] = replace_message_vars(
                notification_dict["displayname"][locale], entry, locale
            )
            notification_dict["description"][locale] = replace_message_vars(
                notification_dict["description"][locale], entry, locale
            )
        # Get Notification Request Images
        attachments_path = (
            settings.spaces_folder
            / f"{settings.management_space}/{branch_path(notification_dict['branch_name'])}"
            f"/{notification_dict['subpath']}/.dm/{notification_dict['shortname']}"
        )
        notification_attachments = await get_entry_attachments(
            subpath=f"{notification_dict['subpath']}/{notification_dict['shortname']}",
            branch_name=notification_dict["branch_name"],
            attachments_path=attachments_path,
        )
        notification_images = {
            "en": notification_attachments.get("media", {}).get("en"),
            "ar": notification_attachments.get("media", {}).get("ar"),
            "ku": notification_attachments.get("media", {}).get("ku"),
        }

        return {
            "platforms": notification_dict["types"],
            "title": Translation(**notification_dict["displayname"]),
            "body": Translation(**notification_dict["description"]),
            "images_urls": Translation(**notification_images),
            "push_only": notification_dict.get("push_only", False),
        }
