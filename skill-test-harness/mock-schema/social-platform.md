# Mock Schema: Social Platform

This schema supports testing user retention, activation, content consumption, and social interaction analysis.

## users

| Field | Type | Description |
| --- | --- | --- |
| user_id | bigint | Unique user id |
| signup_time | datetime | User registration time |
| signup_date | date | User registration date |
| signup_channel | varchar | Acquisition channel |
| device_type | varchar | Device type |
| region | varchar | User region |
| is_test_user | boolean | Whether the user is an internal or test account |

## sessions

| Field | Type | Description |
| --- | --- | --- |
| session_id | bigint | Unique session id |
| user_id | bigint | User id |
| session_start_time | datetime | Session start time |
| session_date | date | Session date |
| duration_seconds | int | Session duration |

## events

| Field | Type | Description |
| --- | --- | --- |
| event_id | bigint | Unique event id |
| user_id | bigint | User id |
| event_time | datetime | Event timestamp |
| event_date | date | Event date |
| event_name | varchar | Event name, such as follow, like, comment, share, publish_post |
| content_id | bigint | Related content id, nullable |

## content_views

| Field | Type | Description |
| --- | --- | --- |
| view_id | bigint | Unique view id |
| user_id | bigint | User id |
| content_id | bigint | Content id |
| view_time | datetime | View timestamp |
| view_date | date | View date |
| stay_seconds | int | Content stay time |

## conversions

| Field | Type | Description |
| --- | --- | --- |
| conversion_id | bigint | Unique conversion id |
| user_id | bigint | User id |
| conversion_time | datetime | Conversion timestamp |
| conversion_date | date | Conversion date |
| conversion_type | varchar | Conversion type |

