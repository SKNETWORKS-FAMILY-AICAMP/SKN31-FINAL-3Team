from setuptools import find_packages, setup


setup(
    name="biddingflow_rfq_portal",
    version="0.1.0",
    description="BiddingFlow extensions for the ERPNext RFQ supplier portal",
    author="BiddingFlow",
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
)
