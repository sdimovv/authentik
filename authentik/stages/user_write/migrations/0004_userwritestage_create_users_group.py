# Generated by Django 3.2.7 on 2021-09-14 19:27

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("authentik_core", "0028_alter_token_intent"),
        ("authentik_stages_user_write", "0003_userwritestage_create_users_as_inactive"),
    ]

    operations = [
        migrations.AddField(
            model_name="userwritestage",
            name="create_users_group",
            field=models.ForeignKey(
                default=None,
                help_text="Optionally add newly created users to this group.",
                null=True,
                on_delete=django.db.models.deletion.SET_DEFAULT,
                to="authentik_core.group",
            ),
        ),
    ]
