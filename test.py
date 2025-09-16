from get_param import ParamGetter
API_ENDPOINT = "http://upiscium.f5.si:8001"

if __name__ == "__main__":
    getter = ParamGetter(API_ENDPOINT)
    user_id = "705c0fb8-503b-4b7c-bc96-065b93a7c79f"
    param = getter.get_param(user_id)
    # print(param)
    print(f"che:{param['chewiness']}, fir:{param['firmness']}")