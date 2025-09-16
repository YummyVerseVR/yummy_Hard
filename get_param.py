import requests
import json

class ParamGetter:
    def __init__(self, db_base: str):
        self.db_base = db_base

    def get_param(self, user_id: str) -> str:
        try:
            response = requests.get(f"{self.db_base}/{user_id}/param")
            if response.status_code == 200:
                return json.loads(response.text)
            else:
                raise Exception(f"Failed to get param for user_id {user_id}, status code: {response.status_code}")
        except Exception as e:
            raise Exception(f"Error: {user_id}: {e}")
        

