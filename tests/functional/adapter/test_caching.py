from dbt.tests.adapter.caching.test_caching import (
    BaseCachingLowercaseModel,
    BaseCachingUppercaseModel,
    BaseNoPopulateCache,
    BaseCachingSelectedSchemaOnly,
)


class TestCachingLowerCaseModel(BaseCachingLowercaseModel):
    pass


class TestCachingUppercaseModel(BaseCachingUppercaseModel):
    pass


class TestCachingSelectedSchemaOnly(BaseCachingSelectedSchemaOnly):
    pass


class TestNoPopulateCache(BaseNoPopulateCache):
    pass
