# Generated by Django 4.1.7 on 2023-04-02 14:19

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("authentik_stages_authenticator_sms", "0005_authenticatorsmsstage_mapping"),
    ]

    operations = [
        migrations.AddField(
            model_name="authenticatorsmsstage",
            name="friendly_name",
            field=models.TextField(null=True),
        ),
    ]
